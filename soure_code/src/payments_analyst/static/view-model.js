export const statuses = {
  ACTIONABLE: ['Investigate first', 'positive'],
  NO_CLEAR_DECLINE: ['No clear decline', 'neutral'],
  INSUFFICIENT_EVIDENCE: ['Insufficient evidence', 'warning'],
  NOT_PROFITABLE: ['Unprofitable recovery', 'negative'],
  UNCERTAIN_FINANCIAL_IMPACT: ['Uncertain impact', 'warning'],
  UNPRICED: ['Pricing unavailable', 'warning'],
  NO_RETRY_ELIGIBLE_ORDERS: ['No eligible failures', 'neutral'],
};
export const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
export const percent = value => Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '—';
export const number = value => Number.isFinite(value) ? new Intl.NumberFormat('en-IN', {maximumFractionDigits: 1}).format(value) : '—';
export const money = value => Number.isFinite(value) ? new Intl.NumberFormat('en-IN', {style:'currency', currency:'INR', maximumFractionDigits:2}).format(value / 100) : '—';
export function toPaise(value) {
  if (!/^\d+(?:\.\d{0,2})?$/.test(String(value))) throw new Error('Enter a non-negative cost with at most two decimal places.');
  const [whole, fraction = ''] = String(value).split('.');
  const result = Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
  if (!Number.isSafeInteger(result) || result > 1_000_000_000) throw new Error('Intervention cost exceeds the supported limit.');
  return result;
}
export function currentWindow(dataset) {
  if (!dataset?.current_start || !dataset?.window_end) return 'Selected snapshot · IST';
  const first = new Date(dataset.current_start), last = new Date(new Date(dataset.window_end).getTime() - 1);
  const fmt = value => new Intl.DateTimeFormat('en-GB', {day:'numeric', month:'short', timeZone:'Asia/Kolkata'}).format(value);
  return `${fmt(first)} – ${fmt(last)} ${new Intl.DateTimeFormat('en', {year:'numeric', timeZone:'Asia/Kolkata'}).format(last)} · IST`;
}
export const segmentLabel = segment => `${segment.bank} / ${segment.payment_method === 'CARD' ? 'Card' : segment.payment_method}`;
