import { useEffect, useRef, useState } from "react";

import { ApiError, api } from "./api";
import type { GroundedNarrative, ImpactBand, ReportDetail, ReportHorizon, StatementOrigin } from "./types";

const COPY = {
  en: {
    saved: "Saved report", full: "Open full reading mode", close: "Close reading mode",
    overall: "Overall impact", occupation: "Occupation summary", horizons: "How work may change",
    tasks: "Task impact matrix", task: "Task", horizon: "Time horizon", automation: "Automation",
    augmentation: "Augmentation", human: "Human-led", rationale: "Why", opportunities: "Opportunities",
    risks: "Risks and uncertainty", actions: "Practical next actions", uncertainty: "Uncertainty",
    citations: "Sources and passages", expand: "Expand all sources", collapse: "Collapse all sources",
    noCitations: "No local source passages are cited in this report. Read the evidence basis shown with each statement.",
    reviewer: "Reviewer decisions", accepted: "accepted", rejected: "rejected", decisions: "View decision reasons",
    authority: "Qwen 30B made the final decisions on suggestions from the three Qwen 4B review roles.",
    scope: "These are scenarios about changes to work, not a personal unemployment probability.",
    relevance: "Used in this report", passage: "Source passage", source: "Source", claims: "Analysis basis",
    sourceScope: "This citation refers to the source as a whole; no specific source passage is cited.",
  },
  zh: {
    saved: "已保存的报告", full: "完整阅读", close: "关闭阅读模式",
    overall: "整体影响程度", occupation: "职业概况", horizons: "未来工作变化",
    tasks: "任务影响矩阵", task: "工作任务", horizon: "时间范围", automation: "自动化",
    augmentation: "AI 辅助", human: "人主导", rationale: "依据", opportunities: "机会",
    risks: "风险与不确定性", actions: "下一步行动", uncertainty: "不确定性",
    citations: "来源与原文", expand: "展开全部来源", collapse: "收起全部来源",
    noCitations: "本报告没有引用本地来源片段。请留意每条陈述所标明的分析依据。",
    reviewer: "审核建议决议", accepted: "采纳", rejected: "拒绝", decisions: "查看决议理由",
    authority: "三项 Qwen 4B 审核角色仅提供建议，最终决议均由 Qwen 30B 作出。",
    scope: "本报告讨论工作变化的可能情景，不提供个人失业概率。",
    relevance: "本报告中的用途", passage: "来源原文", source: "来源", claims: "分析依据",
    sourceScope: "来源级引用，未指定具体原文段落。",
  },
};

const BANDS: Record<"en" | "zh", Record<ImpactBand, string>> = {
  en: { low: "Low", medium: "Medium", "medium-high": "Medium–high", high: "High" },
  zh: { low: "低", medium: "中", "medium-high": "中高", high: "高" },
};
const HORIZONS: Record<"en" | "zh", Record<ReportHorizon, string>> = {
  en: { "1-3-years": "1–3 years", "3-5-years": "3–5 years" },
  zh: { "1-3-years": "未来 1–3 年", "3-5-years": "未来 3–5 年" },
};
const ORIGINS: Record<"en" | "zh", Record<StatementOrigin, string>> = {
  en: { profile: "Your profile", rag: "Local evidence", reasoned_scenario: "Reasoned scenario", recommendation: "Recommendation" },
  zh: { profile: "用户资料", rag: "本地证据", reasoned_scenario: "推演情景", recommendation: "行动建议" },
};

function ReportBody({ detail }: { detail: ReportDetail }) {
  const report = detail.report;
  const language = report.language;
  const copy = COPY[language];
  const citationNodes = useRef(new Map<string, HTMLDetailsElement>());
  const citationIndices = new Map(report.citations.map((citation, index) => [citation.evidence_ref, index + 1]));
  const citationDetails = new Map(detail.citation_details.map((citation) => [citation.evidence_ref, citation]));
  const accepted = report.suggestion_resolutions.filter((resolution) => resolution.decision === "accepted").length;
  const rejected = report.suggestion_resolutions.length - accepted;

  function openCitation(reference: string) {
    const node = citationNodes.current.get(reference);
    if (!node) return;
    node.open = true;
    node.scrollIntoView({ behavior: "smooth", block: "nearest" });
    node.querySelector("summary")?.focus({ preventScroll: true });
  }

  function Narrative({ value }: { value: GroundedNarrative }) {
    return (
      <div className="report-narrative">
        <p>{value.text}</p>
        <div className="narrative-basis">
          <span>{ORIGINS[language][value.origin]}</span>
          {value.evidence_refs.map((reference) => (
            <button key={reference} type="button" className="citation-link"
              onClick={() => openCitation(reference)}
              aria-label={`${copy.source} ${citationIndices.get(reference) ?? ""}`}>
              [{citationIndices.get(reference)}]
            </button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="report-content" lang={language}>
      <div className="report-impact"><span>{copy.overall}</span><strong>{BANDS[language][report.overall_impact_band]}</strong></div>
      <p className="report-scope">{copy.scope}</p>
      <section className="report-section">
        <h3>{copy.occupation}</h3>
        <Narrative value={report.sections.occupation_summary.summary} />
      </section>
      <section className="report-section">
        <h3>{copy.horizons}</h3>
        <div className="report-horizons">
          {report.sections.horizon_scenarios.map((scenario) => (
            <article key={scenario.horizon}>
              <h4>{HORIZONS[language][scenario.horizon]}</h4>
              <span className="impact-band">{copy.overall}: {BANDS[language][scenario.impact_band]}</span>
              <Narrative value={scenario.summary} />
              <strong className="report-small-heading">{copy.uncertainty}</strong>
              <Narrative value={scenario.uncertainty} />
            </article>
          ))}
        </div>
      </section>
      <section className="report-section">
        <h3>{copy.tasks}</h3>
        <div className="report-table-scroll" role="region" aria-label={copy.tasks} tabIndex={0}>
          <table className="report-task-table">
            <thead><tr>
              <th scope="col">{copy.task}</th><th scope="col">{copy.horizon}</th>
              <th scope="col">{copy.automation}</th><th scope="col">{copy.augmentation}</th>
              <th scope="col">{copy.human}</th><th scope="col">{copy.rationale}</th>
            </tr></thead>
            <tbody>{report.sections.task_impact_matrix.map((row) => (
              <tr key={row.row_id}>
                <th scope="row"><Narrative value={row.task} /></th>
                <td>{HORIZONS[language][row.horizon]}</td>
                <td>{BANDS[language][row.automation]}</td>
                <td>{BANDS[language][row.augmentation]}</td>
                <td>{BANDS[language][row.human_led]}</td>
                <td><Narrative value={row.rationale} /></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      </section>
      <section className="report-section"><h3>{copy.opportunities}</h3><Narrative value={report.sections.opportunities.summary} /></section>
      <section className="report-section"><h3>{copy.risks}</h3><Narrative value={report.sections.risks_and_uncertainty.summary} /></section>
      <section className="report-section">
        <h3>{copy.actions}</h3>
        <ol className="report-actions">{report.sections.practical_next_actions.map((action) => (
          <li key={action.action_id}>
            <Narrative value={action.action} />
            <strong className="report-small-heading">{copy.rationale}</strong>
            <Narrative value={action.rationale} />
          </li>
        ))}</ol>
      </section>
      <section className="report-section report-sources">
        <div className="report-section-heading"><h3>{copy.citations}</h3>
          {report.citations.length > 0 && <div className="source-controls">
            <button type="button" className="text-button" onClick={() => citationNodes.current.forEach((node) => { node.open = true; })}>{copy.expand}</button>
            <button type="button" className="text-button" onClick={() => citationNodes.current.forEach((node) => { node.open = false; })}>{copy.collapse}</button>
          </div>}
        </div>
        {report.citations.length === 0 && <p>{copy.noCitations}</p>}
        {report.citations.map((citation, index) => {
          const source = citationDetails.get(citation.evidence_ref);
          return (
            <details key={citation.evidence_ref} className="report-citation" ref={(node) => {
              if (node) citationNodes.current.set(citation.evidence_ref, node);
              else citationNodes.current.delete(citation.evidence_ref);
            }}>
              <summary>[{index + 1}] {source?.source_title ?? copy.source}{source?.locator ? ` · ${source.locator}` : ""}</summary>
              {source && <div className="citation-detail">
                <p className="citation-metadata">{[source.publisher, source.release_date, source.licence].filter(Boolean).join(" · ")}</p>
                {source.citation_scope === "source" ? <p>{copy.sourceScope}</p> : <>
                  <strong className="report-small-heading">{copy.passage}</strong>
                  <blockquote>{source.text}</blockquote>
                </>}
              </div>}
              <div className="citation-detail"><strong className="report-small-heading">{copy.relevance}</strong><p>{citation.relevance.text}</p></div>
            </details>
          );
        })}
      </section>
      <section className="report-section report-review">
        <h3>{copy.reviewer}</h3>
        <p>{accepted} {copy.accepted} · {rejected} {copy.rejected}</p>
        <p className="report-scope">{copy.authority}</p>
        {report.suggestion_resolutions.length > 0 && <details>
          <summary>{copy.decisions}</summary>
          <ul>{report.suggestion_resolutions.map((resolution) => (
            <li key={resolution.suggestion_id}><strong>{resolution.decision === "accepted" ? copy.accepted : copy.rejected}: </strong>{resolution.reason}</li>
          ))}</ul>
        </details>}
      </section>
      <details className="report-claims">
        <summary>{copy.claims}</summary>
        {report.claims.map((claim) => <article key={claim.claim_id}>
          <Narrative value={{ text: claim.text, origin: claim.basis, evidence_refs: claim.evidence_refs }} />
          <p className="claim-uncertainty">{copy.uncertainty}: {claim.uncertainty}</p>
        </article>)}
      </details>
    </div>
  );
}

export function ReportCard({ reportId, onUnauthorized }: { reportId: string; onUnauthorized: () => void }) {
  const [detail, setDetail] = useState<ReportDetail | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [reading, setReading] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setDetail(null);
    setError("");
    void api.getReport(reportId, controller.signal).then((result) => {
      if (active) setDetail(result);
    }).catch((cause: unknown) => {
      if (!active || controller.signal.aborted) return;
      if (cause instanceof ApiError && cause.status === 401) unauthorized.current();
      else setError("The saved report could not be loaded. Reconnect and try again.");
    });
    return () => { active = false; controller.abort(); };
  }, [reportId, attempt]);

  useEffect(() => {
    if (reading && dialog.current && !dialog.current.open) dialog.current.showModal();
    if (!reading && dialog.current?.open) dialog.current.close();
  }, [reading]);

  if (error) return <div className="report-load-error" role="alert"><p>{error}</p><button type="button" className="text-button" onClick={() => setAttempt((value) => value + 1)}>Load saved report again</button></div>;
  if (!detail) return <p role="status">Loading saved report…</p>;
  const copy = COPY[detail.report.language];

  return (
    <section className="saved-report" lang={detail.report.language}>
      <div className="saved-report-header">
        <span>{copy.saved}</span>
        <h2>{detail.report.title}</h2>
        <button type="button" className="secondary-button" onClick={() => setReading(true)}>{copy.full}</button>
      </div>
      <ReportBody detail={detail} />
      <dialog ref={dialog} className="report-reader" aria-label={detail.report.title} onCancel={() => setReading(false)} onClose={() => setReading(false)}>
        {reading && <>
          <header><h2>{detail.report.title}</h2><button type="button" autoFocus className="secondary-button" onClick={() => setReading(false)}>{copy.close}</button></header>
          <ReportBody detail={detail} />
        </>}
      </dialog>
    </section>
  );
}
