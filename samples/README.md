# 可选测试样本（samples/）

本目录**默认不存在**，仓库的所有测试在不提供样本的情况下也能全部通过
（依赖样本的 2 项测试会自动 skip）。

如需跑真实语料回归，准备以下任意文件，并用环境变量或默认路径指给测试：

| 文件 | 用途 | 环境变量 |
|---|---|---|
| `verify_cache.json` | 番号 → 已验证磁力明细的缓存 | `LMM_VERIFY_CACHE` |
| `library.db` | 旧版索引库（含 `btih` hash 列） | `LMM_LEGACY_LIBRARY_DB` |
| `final_list.tsv` | 可删文件清单（code / GB / magnets / date / path） | `LMM_FINAL_LIST` |

示例：

```bash
export LMM_VERIFY_CACHE=/path/to/verify_cache.json
export LMM_LEGACY_LIBRARY_DB=/path/to/library.db
export LMM_FINAL_LIST=/path/to/final_list.tsv
python -m pytest tests/ -q
```

这三项只用于测试断言，不会被写回、不会上传，也不会被提交（见 `.gitignore`）。
