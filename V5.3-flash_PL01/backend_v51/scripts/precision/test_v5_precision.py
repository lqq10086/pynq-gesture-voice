"""Contract regression for the reused two-class package generator; no board I/O."""
import tempfile
import unittest
from pathlib import Path
import build_precision as bp
from v5_sources import SOURCE, select_calibration
from inference_reference import preprocess, decode

MODEL = Path(r'D:\Tools\work\AI\V5 model\gesture_yolov5n_384input_2output\gesture_yolov5n_384input_2output\weights\gesture_best.onnx')


class PackingContract(unittest.TestCase):
    def test_actual_21_channel_heads_keep_two_class_metadata(self):
        model, shapes, init, ops = bp.graph(MODEL)
        names = ['images'] + [o['output_tensor'] for o in ops]
        # Synthetic scale data tests packing only; never used as calibration.
        cal = {'maximum': dict.fromkeys(names, 1.), 'percentile': dict.fromkeys(names, 1.)}
        configured, params, info = bp.parameters(ops, init, cal)
        with tempfile.TemporaryDirectory() as folder:
            bp.save_package(folder, configured, params, info, shapes, 'TEST_ONLY', MODEL)
            manifest = bp.read(Path(folder) / 'model_manifest.json')
            self.assertEqual(manifest['classes'], ['open_palm', 'pinch_point'])
            self.assertEqual(manifest['detect']['channels_per_anchor'], 7)
            self.assertEqual([h['shape'] for h in manifest['head_outputs']],
                             [[1, 21, h, h] for h in (48, 24, 12)])
            for op in configured:
                if op['op_type'] == 'ConvLinear':
                    self.assertEqual(params['w%02d' % op['index']].shape[0], 21)

    def test_train_selection_is_order_independent_unique_and_disjoint(self):
        records = bp.read(SOURCE / 'split_manifest.json')['records']
        held_out = {r['image_sha256'] for r in records if r['snapshot_role'] != 'train'}
        a = select_calibration(records, held_out)
        b = select_calibration(list(reversed(records)) + records[:10], held_out)
        self.assertEqual([r['image_sha256'] for r in a], [r['image_sha256'] for r in b])
        self.assertEqual(len(a), 100)
        self.assertEqual(len({r['image_sha256'] for r in a}), 100)
        self.assertTrue(all(r['snapshot_role'] == 'train' for r in a))
        self.assertFalse({r['image_sha256'] for r in a} & held_out)
        self.assertEqual({r['domain'] for r in a}, {'legacy', 'FPGA'})
        self.assertEqual({r['sample_kind'] for r in a},
                         {'target_positive', 'pure_background', 'side_only_hardnegative'})

    def test_stored_orientation_and_two_class_pinch_decode(self):
        frame = bp.np.zeros((384, 384, 3), bp.np.uint8)
        frame[:, :192, 2] = 255
        tensor, geometry = preprocess(frame)
        self.assertEqual(float(tensor[0, 0, 100, 0]), 1.)
        self.assertEqual(float(tensor[0, 0, 100, -1]), 0.)
        raw = bp.np.zeros((1, 9072, 7), bp.np.float32)
        raw[0, 0] = [100, 100, 40, 40, .9, .1, .6]
        result = decode(raw, geometry)
        self.assertEqual([d.label for d in result], ['pinch_point'])
        self.assertAlmostEqual(result[0].confidence, .54, places=6)


if __name__ == '__main__':
    unittest.main()
