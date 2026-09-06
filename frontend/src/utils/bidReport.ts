export function formatBidReport(payload: any, riskLimit = 8): string {
  const structured = payload.structured_data || {}
  let report = '🏆 综合得分：' + payload.weighted_score
    + ' / 100（**结论：' + (structured.verdict || '—') + '**）\n\n📊 各维度：\n'
  for (const dimension of payload.dimensions || []) {
    report += '  • ' + dimension.dimension + '：' + dimension.score + ' 分\n'
  }
  const risks = structured.metrics?.disqualify_risks || []
  if (risks.length) {
    report += '\n⚠️ 废标规则命中 ' + risks.length + ' 项：\n'
    for (const risk of risks.slice(0, riskLimit)) {
      report += '  • [' + risk.severity + '] ' + risk.rule + '：' + risk.detail + '\n'
    }
  }
  if (payload.summary) {
    report += '\n📝 ' + (payload.summary.overall_comment || '')
      + '\n✅ 结论：' + (payload.summary.recommendation || '')
  }
  if (payload.issues?.length) {
    report += '\n\n⚠️ 风险问题（按维度）：\n'
    for (const issue of payload.issues.slice(0, 8)) {
      report += '  • [' + (issue.priority || issue.severity) + '] '
        + (issue.subject || '') + '：' + (issue.description || '') + '\n'
    }
  }
  return report
}
