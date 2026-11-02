# 验证记录

- helper-tests.txt：本包脚本19项隔离行为测试，可用 scripts/test_helpers.py 在普通电脑重跑，无需PYNQ或实板。
- skill-validation.txt：skill-creator规范检查。
- forward-test.md：独立agent用新温度记录器题目试用的完整记录，末尾列出维护者修正。
- forward-logs/：试用时的命令、退出码和输出，用户目录已统一为占位路径。报告提及的 app、fixture 脚本和合成硬件来自当时的临时工作区。
- source-manifest.json：历史工程来源的相对文件名与哈希。它证明本次读取的文件身份，不证明来源工程各阶段均被本次重新验证。

本次记录为脚本和模拟流程验证；项目实板结果见 `../../evidence/reuse_case/`。
