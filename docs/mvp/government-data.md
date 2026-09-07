# 澳洲政府知识库

更新日期：2026-09-06（Australia/Melbourne）。当前资料清单为 **10 份官方原件、26 个 JSON、9,262 个检索段落**。新增职业／行业 AI 明细及目录分区已加载，新的主题检索与证据标注规范也已加载。最终固定版中英文各一次真实 API 完成，背景职责归因核读通过；范围与审核解释局限见 [当前有限 demo 验证](grounded-demo.md)，不能沿用旧版五次结果声称当前稳定性。

补充前，正式工作区没有知识来源或活动快照，以前的模型验收使用可清理的临时资料。首次政府资料导入及其单次中文流程结果保留在下方“历史观察”部分，不能代表当前扩充版本的验证结果。

## 当前资料范围

10 份官方原件共 22,239,920 字节，整理成 26 个 `list[str]` JSON 文件、9,262 个检索段落，每段不超过 890 字符。新增两个 JSON 分别包含 357 个职业和 19 个行业的 AI potential scores。原先 8 份原件的 SHA-256 保持不变；原先 24 个 JSON 的内容哈希保持不变，其中 23 个已移入分类目录，解释说明留在顶层，旧位置没有保留重复文件。以下数字分别描述不同分类或表格，不能相加当作不重复的职业数。

| 内容 | 覆盖范围与版本 | 官方入口 |
| --- | --- | --- |
| ABS 职业描述与职责 | OSCA 2024 v1.0，采用 2025-07-28 修订的类别说明；1,156 个六位职业，包括别名、职责、技能等级和注册说明 | [ABS OSCA 下载](https://www.abs.gov.au/statistics/classifications/osca-occupation-standard-classification-australia/2024-version-1-0/data-downloads) |
| JSA 兼容旧分类的职责 | 冻结的 February 2026 工作簿中，ANZSCO 2013 v1.3 的 358 个四位职业组描述、3,037 条职责；只选描述和任务表 | [原工作簿](https://www.jobsandskills.gov.au/sites/default/files/2026-07/ANZSCO%20Occupation%20data%20-%20February%202026.xlsx) |
| JSA 就业预测 | 358 个非 NFD 四位职业组；May 2025 基线、May 2030 和 May 2035 预测，人数单位为千人 | [Employment Projections](https://www.jobsandskills.gov.au/data/employment-projections) |
| JSA 年度短缺数据与解释报告 | 2025 年评估：ANZSCO 2022 的 916 个职业、OSCA 2024 的 1,022 个职业；全国及八个州/领地评级，另有 17 页 Key Findings | [Occupation Shortage](https://www.jobsandskills.gov.au/data/occupation-shortage) |
| JSA 季度短缺报告 | March quarter 2026，2026-06-03 发布，10 页 | [季度报告原件](https://www.jobsandskills.gov.au/download/19956/occupation-shortage-report-march-2026/4025/occupation-shortage-report-march-2026/pdf) |
| JSA AI 与工作变化研究 | Our Gen AI Transition：2025-09-02 主体 Analysis Papers A–E，157 页；2025-09-30 Final Release 补充篇与技术说明，93 页 | [主体原件](https://www.jobsandskills.gov.au/download/19828/our-gen-ai-transition-analysis-papers/3404/our-gen-ai-transition-analysis-papers/pdf)、[补充篇原件](https://www.jobsandskills.gov.au/download/19846/our-gen-ai-transition-final-release/3462/our-gen-ai-transition-final-release/pdf) |
| JSA 职业／行业 AI 潜力明细 | Interactive Table Data Pack 2025-09-03：ANZSCO v1.3 的 357 个四位职业组，ANZSIC 2006 的 19 个 Division；分数保留原始 0–1 单位 | [交互表原件](https://www.jobsandskills.gov.au/sites/default/files/2025-09/jsa_gen_ai_interactive_table_data_pack_20250903.xlsx) |
| JSA AI 图表数据核对原件 | AP Publication Chart Data 2025-09-30：用于逐项核对上述分数与取得官方行业代码，不另行导入图表统计 | [图表数据原件](https://www.jobsandskills.gov.au/sites/default/files/2025-09/JSA%20Gen%20AI%20Capacity%20Study%20AP%20Publication%20Chart%20Data%2020250930.xlsx) |

## AI 明细的选择与单位

只从 Interactive 工作簿选取 `Occupation!A8:G364` 和 `Industry!A8:C26`，第 7 行为各自表头。每个职业或行业保留一段，附分类版本、原 sheet／行号和官方工作簿 URL。图表工作簿 `APA Figure 2!A5:D361` 与 `APA Table 2!A5:C23` 的对应分数，已分别与 357 个职业和 19 个行业逐键核对一致。行业 A–S 代码来自 `APA Figure 7!B5:C194` 的官方代码／名称配对。

- 职业 augmentation／automation exposure score 是职业内任务的平均增强／自动化潜力；行业分数是按职业构成加权的潜力。它们是**无量纲 0–1 分数**，假定当前 Gen AI 充分采用并发挥潜力，不是失业概率、被自动化任务的百分比或预测裁员比例。没有自动划分高低风险，也没有转换成百分比。
- 职业标准差（SD）描述职业内不同任务分数的差异，保留原分数单位。`Occupation!E39`，即 1419 Other Accommodation and Hospitality Managers 的 augmentation SD，原值为 ` - `；检索段落明确标为 null／缺失，并保留原标记，不补零。两个暴露分数均完整，其余入选 SD 均为数值。行业表没有提供 SD，明确说明未提供。
- 职业表末尾的空行及第 366 行抑制说明不计为职业。资料不足时被抑制的记录不被补造；357 个暴露职业不能被说成涵盖另一职责表的全部 358 个职业组。
- 不选职业 H:M、行业 D:F 的技能变化、流动、情景转换率、个人资料或招聘广告占比。图表包 `APA Table 2` 的 D 列表头与交互表定义存在歧义，未采用。图表包中的 2021 Census 人数、top-10 行业内职业统计没有作为新的知识段落导入。

具体定义见交互表 `Data_Dictionary` 和 [JSA Exposure 说明](https://www.jobsandskills.gov.au/studies/generative-artificial-intelligence-capacity-study/our-gen-ai-transition-exposure)。原件、选择范围、缺失值、交叉核对结果和哈希均保存在 [数据清单](au-government-data-manifest.json)。

## 目录与检索覆盖

| 目录 | 内容 | JSON 数 | 段落数 |
| --- | --- | ---: | ---: |
| `occupations/` | ABS OSCA 与 JSA ANZSCO 职业描述、职责 | 16 | 5,581 |
| `ai-impact/` | 两份 Gen AI 正文 PDF 的整理文本、新增两个暴露表 | 4 | 1,313 |
| `labour-market/` | 就业预测、短缺表与短缺报告 | 5 | 2,367 |
| 顶层 | 明确标注的本地解释说明 | 1 | 1 |
| 合计 |  | 26 | 9,262 |

分析检索先绑定一次不可变快照，各主题使用同一快照；范围过滤发生在稀疏检索和向量检索的 top-k 之前。查询配额上限为职业 4 段、AI 影响 2 段、劳动力市场 2 段。职业候选名用于主题检索，完整的职业／职责／行业查询也用于补充职业任务上下文。

**4/2/2 是查询预算，不是实际主题覆盖保证。** 未分类的手工资料仍可参与职业查询并占用其配额；跨主题去重或来源不足也可能使最终结果少于 8 段。`query_counts` 记录各查询槽位实际选入数量，`topic_counts` 按最终段落的来源目录统计真正的分类数量，另记录 `unclassified_count` 与 `missing_topics`。目录分类只表明资料所属主题，不自动证明段落相关、权威或支持某项结论；当前职责也不能单独证明未来 AI 采用或影响。

## 数据口径

- OSCA 2024、ANZSCO 2013 v1.3、ANZSCO 2022 分开标注，没有仅按数字代码合并。未采用仍在征求意见的分类草案。
- 就业预测是职业组趋势，**官方明确尚未纳入生成式 AI 等新兴技术的影响**。2025–2030／2035 不是从今天开始滚动的 1–3 年或 3–5 年预测，不能改写成个人失业概率。每条预测均附此限制。
- 短缺评级说明特定时点的招聘困难，不能解释为求职成功保证或 AI 风险等级。州/领地评级没有误用为全国评级。
- 未导入 ANZSCO 工作簿中混合年份的就业、定制人口普查和薪酬统计。下载文件目录日期与 HTTP 修改时间没有冒充正式发布日期；无法确认具体发布日期的字段保持空值。
- 原 PDF 共 277 个物理页，原件完整保留。检索版排除已核对的 39 页纯封面、目录、参考文献及无正文页面，保留 238 页正文、技术附录和术语说明；具体页号在清单中。正文与引文混合页仍保留，没有整页误删结论。文本保留原物理页码、规范化空白并分段；没有解读图像，复杂表格仍应回查原页。
- 每段官方资料保留职业代码或报告标题、版本、原表行／物理页码和官方网址。当前应用仍使用通用的“本地导入”来源元数据；正式发布者与日期保存在段落和清单中，没有伪造系统的官方来源标签。

## 历史观察：首次 8 份资料版本

以下均为扩充到 10 份原件／26 个 JSON **之前**的观察。该历史版本共 8 份官方原件、10,023,363 字节，整理成 24 个 JSON、8,886 个段落；它的检索结果、运行耗时和通过次数不沿用为当前版本的验收结论。

### 当时的中英文检索修正

初次以原有英文向量模型重建成功，但中文职业查询明显偏题。移除无效的中文关键词索引笔记后，改用本地 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，固定版本 `e8f8c211226b894fcb81acc59f3b34ba3efd5f42`，重新嵌入全部资料。它只负责检索，30B 决策、共享 4B 审核、Whisper 和统一 Metal 锁保持原设计。

该模型采用已有的离线 Transformers 加均值池化路径，当时未安装新的运行依赖。它输出 384 维向量；模型原件约 485 MB，来源与哈希单独记录在 [模型清单](multilingual-embedding-manifest.json)，不计入政府资料原件数量。[模型作者说明](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2)

当时的长资料查询还暴露出通用工具名干扰：中文会计资料里的 `Excel` 把英文参考文献页排到前列。随后把分析检索优先限定为职业、职责和行业，缺失时才使用技能、地区和目标；完整资料仍提供给 30B 决策。该历史观察中职业相关性改善，但依赖细分技能或地区的查询仍可能损失召回。

该历史版本清洗后实际检查了 11 组查询、88 条结果，全部可解析到同一活动快照的原文。它覆盖英文软件、会计、护理、电工，中文软件、会计、护理，以及 AI 任务影响、会计就业预测、护士短缺和真实确认资料查询。各组首条结果均与目标职业或主题相关；会计完整资料查询返回会计文员、普通会计、记账与财务职责，部分后续结果仍包含邻近或其他职业。这是有限的相关性观察，不是完整准确率评估。

该历史版本的真实中文分析结果见 [首次政府资料验证记录](au-government-data-validation.json)，与更早的固定五场景稳定性验收分别记录。当前扩充版本的真实中英文结果由 `grounded-demo` 独立记录。

### 当时的单次中文流程与内容局限

历史 8 份资料版本的最终清洗快照上，一份真实中文报告完成 9/9 阶段并保存，用时 **299.163 秒**，7 条引用可通过报告接口读取。F1/F2/F3 均首次通过；安全审核一次 `truncated` 后在原有第二次尝试中恢复。完整最终校验实际执行，错误数为 0。该耗时与单次通过只属于当时版本，不能作为当前扩充版本的 API 或 demo 通过证据。

参考文献页误召回已在该历史报告中消除，但内容核读仍发现模型把未来 AI 影响、技能建议标成了直接 RAG 支持，引用的当时职责并不能证明这些预测。风险段还重复了职业概述。增加提示说明没有在那次观察中解决这些语义问题；结构校验通过、引用能解析都不能代替证据支持性判断。该报告不证明预测准确，也不计为五次稳定性通过。

那次验收读取器的 `ValidationError` 出现在将 JSON 数组以严格 Python 模式重建 `Suggestion` 的 tuple 字段时；模型流程当时已经完成并保存报告。改用已有 JSON 模式读取后，完整 validator 和决议集合复算通过，报告哈希与先前成功的 API 读取一致。该处理未重跑模型、未放宽合同；它仅解释那次读取异常，不能用于推断其他已清理数据库中的异常原因。

### 当时的失败、修正和测试记录

首次政府知识库的第一个真实中文 API 场景在 F1 失败：两次决议理由都触发已有的 `UNSTRUCTURED_NUMERIC` 规则，未保存报告；阶段、尝试次数、结构位置及 token 指标均已保留。随后只在 F1 的生成 schema 中禁止理由包含数字字符，使模型在生成时遵守已有的定性理由要求。建议 ID、采纳／拒绝选择、完整最终校验、输出上限和两次尝试边界均保持原样；该约束不替代对中英文数字词的原有校验。

第二次真实中文流程 9/9 完成，但人工核读发现参考文献引用以及把未来推断标为直接 RAG 事实的问题，因此只记为软件流程通过。除检索和语料清洗外，当时也在 30B 的 draft/F2 中明确：当前职责不能单独证明未来 AI 采用、影响和时间范围；模型应分别标注直接证据、情景推断与行动建议。没有在后端替模型改写结论或引用。

该历史修正没有新增或修改测试源码，既有 **134 项测试通过（1.94 秒）**；当时存储/UI 的 `MvpReportV1` schema 未变化，前端没有改动，未重复执行前端构建。这些测试数量、时间和 API 观察均不代表当前扩充版本的检查结果。更早的五场景通过记录也属于当时的版本与临时数据。

## 保存位置与更新

- 原件：`var/au-government/2026-09-06/`，新增两个 AI 工作簿位于其 `ai-detail/` 子目录；政府 XLSX/PDF 未改写。
- 可检索资料：`data/knowledge/inbox/au-government-2026-09-06/`，按上方三个主题目录组织。
- 当前活动快照、向量和来源：正常应用数据库 `var/localcareerimpact.sqlite3`。
- [数据清单](au-government-data-manifest.json) 记录直接下载地址、年份、选择的表、转换方式、原件及整理文件 SHA-256。
- [首次政府资料验证记录](au-government-data-validation.json) 保留历史 8 份资料版本的重建、检索和模型标识；当前扩充版的真实中英文 API 验收另记于 `grounded-demo`。首次使用原英文向量模型时，中文检索偏题的原观察留在本地原件目录，没有覆盖成成功。

这些数据和本地模型路径不提交到 Git；本机重启后可直接读取当前快照。在同机更新原资料后，通过管理员知识库页面重建即可。换机器时需重新准备原件、整理资料和模型，再重建；仅拉取代码不会自动得到这批数据。

## 署名和许可

Based on Australian Bureau of Statistics and Jobs and Skills Australia material. **© Commonwealth of Australia.** 原件对应官方 CC BY 4.0 说明；本地版本进行了文本提取、格式规范化、表格选择及分段，未表示政府为本项目背书。未提取图片、标志、微观数据或定制人口普查／薪酬表；第三方内容、图像和商标等例外仍以原件和官方说明为准。[ABS 条款](https://www.abs.gov.au/website-privacy-copyright-and-disclaimer)、[JSA 条款](https://www.jobsandskills.gov.au/copyright-and-disclaimer)、[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
