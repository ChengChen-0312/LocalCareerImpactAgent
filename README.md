# LocalCareerImpactAgent

在 Mac 本机运行的职业影响分析演示：登录后通过文字或附件建立候选人资料，确认后由本地 Qwen 模型生成带知识引用的中英文报告。

首次使用请按 [操作指南](docs/mvp/operator-guide.md) 准备账户、模型目录和依赖，然后在项目根目录运行：

```bash
bash scripts/run-local.sh
```

入口固定为 `http://127.0.0.1:8000`。启动时加载两个本地模型；终端出现网址不代表模型已经就绪，请等待页面的就绪状态。

远程演示需要另行手动运行 `scripts/run-remote-demo.sh` 并在交互终端确认。结束后运行 `scripts/stop-remote-demo.sh`。仅启动本地入口时不会打开 tunnel。

这是私下演示用的 MVP。实际检查结果以 [验收记录](docs/mvp/acceptance-record.md) 为准；记录中仍待验证的项目不能视为通过。模型报告用于探索职业任务变化，不代表已验证的预测准确性或个人失业概率。

## 随仓库提供的数据库

2026-09-07 按项目所有者要求加入完整 SQLite 备份：
[`data/database/localcareerimpact.sqlite3.gz`](data/database/localcareerimpact.sqlite3.gz)。
包含现有会话、提取文字、报告、知识分块和快照；仓库保持私有。
恢复步骤和完整性校验值见 [数据库说明](data/database/README.md)。
模型权重、实际账号密码和知识原文件仍在本地；查询已有快照需要配置与快照匹配的 embedding 模型。

本次代码同步来源为 `03d1c54`。详见 [源码快照说明](docs/SOURCE_SNAPSHOT.md)。
