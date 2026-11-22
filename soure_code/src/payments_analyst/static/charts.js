import {escapeHTML as e, money, percent} from './view-model.js';

const color = {green:'#23826b', blue:'#91aac4', red:'#ba766a', grid:'#edf1f2', text:'#92a2a6'};
const text = (x,y,label,anchor='start',fill=color.text,size=12) => `<text x="${x}" y="${y}" text-anchor="${anchor}" fill="${fill}" font-size="${size}" font-family="Segoe UI,Arial,sans-serif">${e(label)}</text>`;
const line = (x1,y1,x2,y2,stroke=color.grid,dash='') => `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${stroke}" ${dash ? `stroke-dasharray="${dash}"` : ''}/>`;
function path(points, x, y, field) {
  let started = false;
  return points.map(point => {
    if (!Number.isFinite(point[field])) { started = false; return ''; }
    const command = started ? 'L' : 'M'; started = true;
    return `${command}${x(point).toFixed(2)},${y(point[field]).toFixed(2)}`;
  }).join(' ');
}
function mount(target, body, title, height=245) {
  target.innerHTML = `<svg viewBox="0 0 680 ${height}" role="img" aria-label="${e(title)}"><title>${e(title)}</title>${body}</svg>`;
}
export function trendChart(target, segment) {
  const points = segment.trend.slice(-21);
  if (!points.length) { target.innerHTML = '<p class="empty-text">No daily observations available.</p>'; return; }
  const left=47, right=660, top=20, bottom=204;
  const values=points.flatMap(p=>[p.success_rate,p.matched_historical_rate]).filter(Number.isFinite);
  const low=Math.max(0, Math.floor(((values.length ? Math.min(...values) : 0.6)-0.05)*10)/10);
  const high=1, y=v=>bottom-(v-low)/(high-low)*(bottom-top);
  const times=points.map(p=>Date.parse(p.date)), first=times[0], last=times.at(-1);
  const x=p=>left+(Date.parse(p.date)-first)/Math.max(1,last-first)*(right-left);
  let body='';
  const current=points.find(p=>p.is_current_window);
  if (current) body+=`<rect x="${Math.max(left,x(current)-8)}" y="${top}" width="${right-Math.max(left,x(current)-8)}" height="${bottom-top}" fill="#f1f8f4"/>`;
  for(let i=0;i<=4;i++){const v=low+(high-low)*i/4;body+=line(left,y(v),right,y(v))+text(left-11,y(v)+4,`${Math.round(v*100)}%`,'end');}
  points.forEach((p,i)=>{if(i%4===0 || i===points.length-1)body+=text(x(p),bottom+25,new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short',timeZone:'UTC'}).format(new Date(p.date)),'middle');});
  body+=`<path d="${path(points,x,y,'success_rate')}" fill="none" stroke="${color.green}" stroke-width="2.5" stroke-linejoin="round"/>`;
  body+=`<path d="${path(points,x,y,'matched_historical_rate')}" fill="none" stroke="${color.blue}" stroke-width="2" stroke-dasharray="5 5"/>`;
  for(const point of points){if(Number.isFinite(point.success_rate))body+=`<circle cx="${x(point)}" cy="${y(point.success_rate)}" r="3" fill="${color.green}"/><circle cx="${x(point)}" cy="${y(point.success_rate)}" r="9" fill="transparent"><title>${e(point.date)} · ${percent(point.success_rate)} · ${point.completed_orders} completed orders</title></circle>`;}
  if(current)body+=text(right-8,top+16,'Current window','end','#7e9b8b',11);
  mount(target,body,`${segment.bank} ${segment.payment_method} completed-order conversion and matched historical baseline. Axis starts at ${Math.round(low*100)} percent.`);
}
export function rankingChart(target, segments) {
  const points=segments.filter(s=>s.reference_scenario);
  if(!points.length){target.innerHTML='<p class="muted">No fully priced, supported reference scenarios are available.</p>';return;}
  const left=130,right=625,top=19,bottom=top+points.length*30;
  const extents=points.flatMap(s=>[...s.net_impact_interval_paise,s.reference_scenario.incremental_net_contribution_paise,0]);
  const min=Math.min(...extents),max=Math.max(...extents),span=Math.max(1,max-min),x=v=>left+(v-min)/span*(right-left);
  let body=line(x(0),top-12,x(0),bottom-8,'#d5dfe0');
  points.forEach((s,i)=>{
    const yy=top+i*30, v=s.reference_scenario.incremental_net_contribution_paise;
    const tint=v<0?color.red:s.rank?color.green:'#b8c8c6';
    body+=text(0,yy+5,`${s.bank} / ${s.payment_method}`,'start','#71878b',12);
    body+=`<rect x="${Math.min(x(0),x(v))}" y="${yy-8}" width="${Math.max(2,Math.abs(x(v)-x(0)))}" height="16" rx="3" fill="${tint}" opacity=".85"><title>${e(s.bank)} ${e(s.payment_method)} · ${e(money(v))} reference net contribution</title></rect>`;
    const [low,high]=s.net_impact_interval_paise;
    body+=line(x(low),yy,x(high),yy,'#708e87')+line(x(low),yy-4,x(low),yy+4,'#708e87')+line(x(high),yy-4,x(high),yy+4,'#708e87');
  });
  body+=text(left,bottom+14,money(min),'start')+text(right,bottom+14,money(max),'end');
  mount(target,body,'Reference net contribution by segment with historical sampling ranges. Exact values and statuses are in the table.',bottom+27);
}
export function simulationChart(target, scenario) {
  const points=scenario.curve,left=59,right=660,top=22,bottom=197;
  const amounts=points.map(p=>p.incremental_net_contribution_paise),rawLow=Math.min(0,...amounts),rawHigh=Math.max(0,...amounts);
  const padding=Math.max(10,(rawHigh-rawLow)*.12),low=rawLow-padding,high=rawHigh+padding;
  const x=p=>left+p.recovery_fraction*(right-left),y=v=>bottom-(v-low)/(high-low)*(bottom-top);
  let body='';
  for(let i=0;i<=4;i++){const v=low+(high-low)*i/4;body+=line(left,y(v),right,y(v))+text(left-9,y(v)+4,new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR',maximumFractionDigits:0}).format(v/100),'end');}
  for(let i=0;i<=4;i++)body+=text(left+(right-left)*i/4,bottom+25,`${i*25}%`,'middle');
  body+=line(left,y(0),right,y(0),'#b9c8c6','4 4');
  body+=`<path d="${path(points,x,y,'incremental_net_contribution_paise')}" fill="none" stroke="${color.green}" stroke-width="2.5" stroke-linecap="round"/>`;
  const selected=scenario.result,px=x(selected),py=y(selected.incremental_net_contribution_paise),fill=selected.incremental_net_contribution_paise<0?color.red:color.green;
  body+=line(px,top,px,bottom,'#aec7bd','3 5');
  body+=`<circle cx="${px}" cy="${py}" r="5" fill="${fill}" stroke="white" stroke-width="2"><title>${selected.recovery_fraction*100}% of gap recovered · ${e(money(selected.incremental_net_contribution_paise))}</title></circle>`;
  const breakEven=selected.break_even_recovery_fraction;
  if(selected.break_even_within_historical_gap && breakEven!==null)body+=`<circle cx="${x({recovery_fraction:breakEven})}" cy="${y(0)}" r="3" fill="white" stroke="#91a99f"><title>Break-even at ${(breakEven*100).toFixed(1)}% of the gap recovered</title></circle>`;
  mount(target,body,'Conditional incremental net contribution versus percentage of the historical gap recovered. The dashed horizontal line marks zero net improvement.');
}
