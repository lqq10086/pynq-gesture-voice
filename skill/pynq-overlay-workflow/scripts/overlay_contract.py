"""Portable contract validation; importing this module never imports PYNQ."""
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import xml.etree.ElementTree as ET


def number(value, label):
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r'(0[xX][0-9a-fA-F]+|[0-9]+)', value):
        result = int(value, 16 if value.lower().startswith('0x') else 10)
    else:
        raise ValueError(label + ': expected integer or hexadecimal string')
    if result < 0:
        raise ValueError(label + ': negative value')
    return result


def required_text(value, label):
    if not isinstance(value, str) or not value.strip() or value.startswith('fill-'):
        raise ValueError(label + ': fill the current project value')
    return value


def local_file(root, value):
    required_text(value, 'artifact path')
    if '\\' in value or Path(value).is_absolute() or PureWindowsPath(value).drive:
        raise ValueError('artifact path must use relative forward-slash syntax')
    path = (root / value).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError('artifact path escapes contract directory')
    if not path.is_file():
        raise ValueError('artifact file missing: ' + value)
    return path


def load_contract(filename):
    path = Path(filename).resolve()
    cfg = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(cfg, dict) or type(cfg.get('schema_version')) is not int or cfg['schema_version'] != 1:
        raise ValueError('schema_version must be 1')
    for key in ('target_label', 'build_id'):
        required_text(cfg.get(key), key)
    artifacts = cfg.get('overlay')
    if not isinstance(artifacts, dict):
        raise ValueError('overlay must be an object')
    files = {k: local_file(path.parent, artifacts.get(k)) for k in ('bit', 'hwh')}
    if (files['bit'].suffix.lower() != '.bit' or files['hwh'].suffix.lower() != '.hwh'
            or files['bit'].parent != files['hwh'].parent or files['bit'].stem != files['hwh'].stem):
        raise ValueError('bit/hwh must share directory and stem')
    hashes = {}
    for key, file in files.items():
        expected = artifacts.get(key + '_sha256')
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected):
            raise ValueError(key + '_sha256: require trusted build hash')
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        if digest != expected.lower():
            raise ValueError(key + ' SHA256 mismatch')
        hashes[key] = digest
    ET.parse(files['hwh'])  # XML syntax only, not hardware design validation.
    ips = cfg.get('ips')
    if not isinstance(ips, list) or not ips:
        raise ValueError('ips must be a nonempty list')
    names = set()
    for ip in ips:
        if not isinstance(ip, dict):
            raise ValueError('IP entry must be an object')
        name = required_text(ip.get('name'), 'IP name')
        if name in names:
            raise ValueError('duplicate IP name: ' + name)
        names.add(name)
        vlnv = required_text(ip.get('type'), 'IP type')
        if len(vlnv.split(':')) != 4 or any(not x for x in vlnv.split(':')):
            raise ValueError('IP type must be vendor:library:name:version')
        required_text(ip.get('evidence'), 'IP evidence')
        ip['phys_addr'] = number(ip.get('phys_addr'), 'phys_addr')
        ip['addr_range'] = number(ip.get('addr_range'), 'addr_range')
        if ip['phys_addr'] % 4 or ip['addr_range'] < 4 or ip['addr_range'] % 4:
            raise ValueError('IP range/base must support aligned 32-bit accesses')
        reads = ip.get('safe_reads')
        if not isinstance(reads, list):
            raise ValueError('safe_reads must be a list')
        for read in reads:
            if not isinstance(read, dict):
                raise ValueError('read entry must be an object')
            required_text(read.get('name'), 'read name')
            required_text(read.get('evidence'), 'read evidence')
            if read.get('side_effect') != 'none':
                raise ValueError('only explicitly side_effect=none reads are supported')
            for key in ('offset', 'mask', 'expected'):
                read[key] = number(read.get(key), 'read ' + key)
            if read['offset'] % 4 or read['offset'] + 4 > ip['addr_range']:
                raise ValueError('read offset unaligned or outside IP range')
            if not 0 < read['mask'] <= 0xffffffff or read['expected'] > 0xffffffff:
                raise ValueError('read mask/expected must fit uint32, mask nonzero')
            if read['expected'] & ~read['mask']:
                raise ValueError('expected contains bits outside mask')
    return cfg, files, hashes
