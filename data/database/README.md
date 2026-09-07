# 项目数据库备份

备份日期：2026-09-07（Australia/Melbourne）。

`localcareerimpact.sqlite3.gz` 是通过 SQLite online backup 从项目数据库取得的一致快照，再以 gzip 无损压缩。它不是空数据库或抽样数据，包含全部现有表，包括聊天、提取文字、资料卡、运行记录、报告、知识分块、向量和知识快照。项目所有者明确要求随本次代码提交上传，仓库保持私有。

| 项目 | 值 |
| --- | --- |
| 未压缩大小 | 218,542,080 bytes |
| 压缩包大小 | 87,121,270 bytes |
| 聊天 / 消息 / 报告 | 29 / 100 / 16 |
| 知识文档 / 分块 / 快照 | 39 / 36,211 / 4 |
| 知识源记录 | 26 |
| SQLite integrity_check | ok |
| foreign_key_check 错误数 | 0 |

未压缩数据库 SHA-256：

```text
c7802473e0b0d56e4e1543aa37228c9e743bf2b16bc6ea123abe531fc8524d4b
```

压缩包 SHA-256：

```text
e62fd498106aab72b4feee900ef24264571d459ba8885747e9e15b325d09c1d0
```

## 恢复

先停止使用目标目录的应用和后台 worker；如果目标目录已有数据库及 WAL/SHM 文件，请先整体备份并移开。下面命令仅用于没有现有数据库的新 checkout，使用 noclobber 防止覆盖主数据库：

```sh
mkdir -p var
(set -C; gzip -dc data/database/localcareerimpact.sqlite3.gz > var/localcareerimpact.sqlite3)
shasum -a 256 var/localcareerimpact.sqlite3
sqlite3 var/localcareerimpact.sqlite3 'PRAGMA integrity_check;'
```

确认校验值匹配后，再按项目操作指南配置模型目录、账号并启动。

备份不依赖原机器的 WAL/SHM 文件；不要将原机器的这两类运行时文件复制到恢复目录。实际账号密码在独立 CSV 中，不在本次上传文件内。

已存储的报告和知识分块可随数据库恢复。查询现有快照需要匹配该快照的 embedding 模型与文件指纹；最新语料使用的多语言模型记录在 `docs/mvp/multilingual-embedding-manifest.json`。原始知识 PDF/TXT 文件仍在本地，数据库中的原文件路径不会自动变为新电脑的有效路径；重新导入或重建知识库前需另行准备原文件。模型权重不随此备份上传。
