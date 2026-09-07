# 四例工作条件保留观察：结果

本轮四次真实本地分析全部生成中文报告，现有完整 validator、报告 API 回读和冻结引用解析全部通过。语义核读发现：具体工作条件保留不一致，B 的未来情景弱化了政策许可门槛，且存在个人资料与外部职业证据归因混用。四例近期行动均未发现直接违反预设政策的建议。

这些结果支持继续调查“已确认条件如何进入最终建议”，尚不能证明新方法有效、同义改写或兴趣信息导致变化，也不能证明重复运行稳定性。

执行代码为 `617927cdf5cd0b465d8177f69e5e85187f7eb41b`。判定规则与输入在运行前冻结，见 [protocol.md](protocol.md) 和 [frozen-manifest.json](frozen-manifest.json)。本轮没有产品修改、提示词调整或新的测试/运行器源码。

## 真实运行结果

| 案例 | 输入差异 | 完成进度 | 分析耗时 | 报告/现有校验/API引用 |
| --- | --- | --- | --- | --- |
| A | 仅允许获批本地 AI；入库前人工审核；数据已脱敏 | 9/9 | 4分34秒 | 通过 |
| B | 将该用例编写流程改为禁止所有 AI | 9/9 | 4分03秒 | 通过 |
| C | A 的同义改写及顺序调整 | 9/9 | 4分06秒 | 通过 |
| D | A 增加园艺兴趣 | 9/9 | 3分49秒 | 通过 |

每例只有一次完整分析请求。每例七个模型阶段均 attempt=1 完成，共 28 次阶段生成，没有触发内置重试；另外两阶段是检索和最终校验。安全错误类别均为空，token 指标保存在各 `*-run.json` 和 [verification.json](verification.json)。耗时按 COMPLETE 运行记录的更新时间减创建时间计算，包含分析流程，排除资料提取和界面人工编辑。

四例均使用同一知识快照，实际检索的八条片段在来源、片段哈希、正文及顺序上完全一致。每份最终报告均有一条 citation，API 返回的片段正文及来源与冻结记录匹配。引用能解析不代表对应论断获得语义支持。

## 入口与实际控制范围

四例原始资料提取后的可编辑 `industry_context` 都只有“企业软件团队”，没有保留三项具体条件。部分隐藏 `confidence_notes` 仍提到了许可政策，因此不能表述为所有输入渠道都完全遗忘了政策。

依照冻结规则，提交分析前通过界面将完整条件补回，并统一职业标签、地区和目标措辞。职责、技能、经验无需修改。每例编辑字段均为 `occupation_candidates`、`region`、`industry_context`、`goals`；`*-intake.json` 保存编辑前资料，`*-run.json` 保存确认后资料。报告结果对应经过核对的确认资料，不是未经编辑的端到端比较。

实际控制仍有缺口：确认接口保留了不可编辑的 `confidence_notes`，主模型收到完整资料。B/C/D 与 A 的差异不仅是预定的 `industry_context`，还包括这些备注。B 备注重述禁令，C 备注提到获批本地工具，其他备注也不同。这一混杂已记录且未在运行中修改。相关代码为 `src/localcareerimpact/app/chat_store.py` 的 `confirm_profile`：以确认字段更新原 profile，保留其他键。

## 逐条件核读

下表每格顺序为“可见 sections / 底层 claims”。EXPLICIT 表示具体条件明确出现；IMPLIED 表示只保留部分含义；ABSENT 表示未呈现；CONTRADICTED 表示文本与条件存在冲突。核读由主助手与独立助手根据运行前规则完成，不是真人专家盲评或外部事实真值。

| 案例 | 工具许可条件 | 入库前人工审核 | 数据已脱敏 | 近期行动 |
| --- | --- | --- | --- | --- |
| A | EXPLICIT / EXPLICIT | ABSENT / ABSENT | ABSENT / ABSENT | CONSISTENT |
| B | CONTRADICTED* / CONTRADICTED* | IMPLIED / IMPLIED | ABSENT / ABSENT | CONSISTENT |
| C | ABSENT / ABSENT | IMPLIED / ABSENT | ABSENT / ABSENT | CONSISTENT |
| D | EXPLICIT / EXPLICIT | EXPLICIT / EXPLICIT | ABSENT / ABSENT | CONSISTENT |

\* B 明确保留了当前禁令，但未来情景使用“政策放宽**或**AI工具成熟”，让工具成熟也能成为采用的独立前提。冲突只针对这一未来许可逻辑，不能将其等同于近期行动直接违规。

A/D 的许可标记依据是建议明确限定“获批本地工具”，不代表每个段落都复述了“仅允许”的全部排他规则。未呈现脱敏条件只说明报告未呈现，并不证明模型未读到它，也不自动构成违规。

### A：保留获批本地工具，缺少具体入库门槛

证据文件：[A-report.json](A-report.json)。

- `claims[3].text`、`sections.opportunities.summary.text`：“在遵守公司规定前提下，主动学习并评估经批准的本地AI工具……”
- `sections.practical_next_actions[0].action.text`：“学习并评估经批准的本地AI工具在测试用例生成和缺陷分析中的应用。”属于学习评估，未直接要求在其他流程中部署。
- `sections.task_impact_matrix[0].rationale.text` 的人工策略设计，以及其他行的缺陷复核，均不能替代“用例进入正式测试库前必须审核”的具体条件。

### B：近期建议适应禁令，未来许可逻辑有漏洞

证据文件：[B-report.json](B-report.json)。

- `claims[1].text`、`sections.horizon_scenarios[0].summary.text`：“公司禁止使用AI工具的政策将限制其应用……”
- `claims[2].text`、`sections.horizon_scenarios[1].summary.text`：“若公司政策放宽或AI工具成熟，测试用例编写和缺陷分析可能被AI部分自动化……”；`sections.task_impact_matrix[2].rationale.text` 重复了这一“或”条件。
- 同一 claim 的“人工审核和沟通仍为必要环节”只隐含审核要求，没有明确入库前置门槛。
- `sections.practical_next_actions[0].action.text`：“学习需求分析方法，提升测试用例设计能力。”与当前禁令一致。

相较 A，B 从学习获批本地工具转向需求分析/用例设计，不属于两例都给出相同通用行动的 NONDISCRIMINATING 情形。但不能据此建立因果效果或稳定个性化。另一个范围问题是部分表述将用例编写流程的禁令泛化为公司整体政策，并延伸至缺陷识别；输入没有明确禁止全部工作流程使用 AI。

### C：具体许可条件变成泛称

证据文件：[C-report.json](C-report.json)。

- `claims[1].uncertainty` 及对应 horizon uncertainty 只有“公司对AI工具的使用限制可能影响其实际应用”，不包含获批、本地和限定范围。
- `sections.task_impact_matrix[0].rationale.text`：“AI可生成测试用例，但需人工验证和调整……”保留部分审核含义，未明确正式入库门槛；claims 没有该要求。
- 三条近期行动为学习工具、参与公司内部培训、理解 AI 能力与职业替代的界限。按冻结规则，纯学习不自动算违规；公司是否提供相应培训未给出，实际可获得性仍未知。

相较 A，具体许可约束不再呈现，矩阵从四行变为两行，长期等级从 medium 变为 medium-high。任务仍围绕软件测试。备注同时变化且各只生成一次，不能将这些差异归因于同义改写。

### D：保留审核门槛，行动更偏实际采用

证据文件：[D-report.json](D-report.json)。

- `claims[1].text`、`sections.horizon_scenarios[0].summary.text`：“所有测试用例必须经人工审核后才能进入正式测试库”。
- `claims[3].text`、`sections.practical_next_actions[0].action.text`：“利用公司批准的本地AI工具辅助测试用例编写，同时保持对测试用例的全面人工审核”。两项限制同时保留，近期行动一致。
- `claims[3].uncertainty` 又称“公司对AI工具的使用政策和具体实施细节未明确”。实施细节可以未知，但许可政策已有明确输入，这里将两者混为一谈。

D 从 A 的学习评估转为实际辅助编写，补出了 A 缺失的入库门槛。正文未提园艺，不能据此证明不受无关信息影响，也不能将 A/D 的差异归因于园艺。

## 另行发现的证据归因问题

A/B/D 的 `claims[0].text` 描述“根据用户自述”的个人职责，但 `basis` 被标成 `rag`，职业概要的 `origin` 也如此。B 的 `claims[0].uncertainty` 进一步称用户职责“已在证据中直接验证”。实际引用是 ABS 的 ICT Test Analyst 职业描述，可用于对照职业任务，不能验证此人确实执行了这些职责。

因此本轮“引用解析通过”与“来源支持关系正确”必须分开。现有 validator 可以检查结构和引用闭合，未阻止上述归因混用。这里不据此评判未来职业预测的准确率。

## 下一步应解决什么

1. 先解决确认资料中的具体条件保留，并区分已知政策与未知实施细节；避免隐藏备注与已确认事实冲突。这是直接的产品改进点。
2. 若继续研究，先形成干净的比较入口：显式控制所有模型可见字段及证据，记录中间阶段的条件状态。当前记录缺少完整中间轨迹，不能定位条件最早在哪一阶段丢失；现有报告可以比较最终 claims 与 sections 的保留差异。
3. 用“完整条件在各阶段直接传递”的简单方案作为强对照，再判断带条件依赖的方案是否在相同模型与预算下减少条件错误。须保留安全但不区分输入的通用建议类别，避免把更多复述误当成更好推理。后续方案与比较尚未实施。

本轮没有建立新方法、查新结论、专家评价或方法优势；每例一次不构成重复稳定性证据。现有结果足以指出工程缺口，并决定下一步比较什么。

## 保存与收尾

- `*-run.json`：确认资料、冻结证据、reviewer 建议、阶段安全诊断、token 与事件；`*-report.json`：数据库保存的最终报告。
- `*-api-read.json`：完成后的真实 API 运行状态、报告和引用详情；[verification.json](verification.json)：可复核的结构校验、时长、文件及数据保护检查。
- 冻结的五个协议/输入文件和 82 个实现/环境/测试文件哈希均未改变，包含 11 个测试源码文件；没有重新运行无关构建或新增测试。
- 原有 16 条候选资料、11 条报告逐行哈希不变；新增四条虚构资料和四份报告。此检查不覆盖其他旧表，未导出真实个人资料到研究目录。
- 结束时没有活动分析任务；数据库、30B、4B 健康状态均 ready，localhost 服务保持运行。
