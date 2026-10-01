const { createApp, ref, computed, watch, onMounted, nextTick } = Vue;
const API = (window.MBON_API_URL || "http://127.0.0.1:5050/api").replace(/\/+$/, "");

/* ── Color helpers ───────────────────────────────────────────────────────── */
const RANK_COLORS = {
  D:'#0077b6',K:'#00b4d8',P:'#48cae4',C:'#90e0ef',
  O:'#f77f00',F:'#fcbf49',G:'#2ec4b6',S:'#e63946',U:'#adb5bd'
};
function rankColor(r){ return RANK_COLORS[r]||'#adb5bd'; }
function rankLabel(r){
  return {D:'Domain',K:'Kingdom',P:'Phylum',C:'Class',O:'Order',F:'Family',G:'Genus',S:'Species',U:'Unclassified'}[r]||r;
}
const GROUP_PALETTE = [...d3.schemeTableau10,...d3.schemePastel1,...d3.schemeSet3];
const _gcm={};let _gi=0;
function groupColor(g){ if(!g)return'#adb5bd'; if(!_gcm[g])_gcm[g]=GROUP_PALETTE[_gi++%GROUP_PALETTE.length]; return _gcm[g]; }
function stnCompareColor(i){ return d3.schemeTableau10[i % d3.schemeTableau10.length]; }
const MAIN_RANKS=new Set(['D','K','P','C','O','F','G','S','U']);
const WILDER_STD_RANKS=new Set(['domain','kingdom','phylum','class','order','family','genus','species']);
const RANK_ORDER=['D','K','P','C','O','F','G','S','U'];
const PREFERRED_RANKS=['S','G','F','O','C','P','K','D','U'];
function sortRanks(ranks){
  return [...new Set((ranks||[]).filter(Boolean))].sort((a,b)=>{
    const ia=PREFERRED_RANKS.indexOf(a), ib=PREFERRED_RANKS.indexOf(b);
    return (ia<0?99:ia)-(ib<0?99:ib) || String(a).localeCompare(String(b));
  });
}
function chooseRank(ranks,current){
  const opts=sortRanks(ranks);
  if(current && opts.includes(current)) return current;
  return PREFERRED_RANKS.find(r=>opts.includes(r)) || opts[0] || '';
}
function fmtCompactNumber(n){
  if(n==null || n==='') return '–';
  const value=Number(n);
  if(!Number.isFinite(value)) return '–';
  const sign=value<0?'-':'';
  const abs=Math.abs(value);
  if(abs<1000) return sign+abs.toLocaleString();
  const units=[
    {v:1e12,s:'T'},
    {v:1e9,s:'B'},
    {v:1e6,s:'M'},
    {v:1e3,s:'k'},
  ];
  const u=units.find(x=>abs>=x.v);
  const scaled=abs/u.v;
  const digits=scaled>=100 ? 0 : scaled>=10 ? 1 : 1;
  return sign+scaled.toFixed(digits).replace(/\.0$/,'')+u.s;
}

/* ── Shared tooltip ──────────────────────────────────────────────────────── */
let _tip=null;
function showTip(e,html){ if(!_tip)return; _tip.innerHTML=html; _tip.style.opacity=1; moveTip(e); }
function moveTip(e){ if(!_tip)return; _tip.style.left=Math.min(e.clientX+14,window.innerWidth-300)+'px'; _tip.style.top=Math.max(e.clientY-12,8)+'px'; }
function hideTip(){ if(_tip)_tip.style.opacity=0; }

/* ── D3 horizontal bar chart ─────────────────────────────────────────────── */
function drawHBar(svgEl, wrapEl, data, opts={}){
  if(!svgEl||!wrapEl||!data.length)return;
  const { labelKey='name', valueKey='value', colorFn=()=>'#0077b6',
    fmtVal=v=>d3.format(',.0f')(v), fmtTip, pct=false, italic=false,
    onClickBar=null, highlightSet=null, zoomKey=null } = opts;

  const filteredData = data.filter(d => +d[valueKey] > 0);
  if(!filteredData.length)return;

  const hasHL = highlightSet && highlightSet.size > 0;
  const opacOf = d => !hasHL ? 1 : highlightSet.has(d[labelKey]) ? 1 : 0.25;

  wrapEl.style.overflowX = '';
  svgEl.style.width = '';

  const W = wrapEl.clientWidth||700;
  const BH=22, pad={top:8,right:100,bottom:30,left:220};
  const naturalInnerH = filteredData.length*(BH+4);
  // Cap visible height so that zoom/scroll always reveals hidden bars.
  // Show ~70% of bars initially (min 220px, max 400px). For ≤6 bars show all.
  const innerH = filteredData.length <= 6
    ? naturalInnerH
    : Math.max(220, Math.min(400, Math.ceil(filteredData.length * 0.70) * (BH+4)));
  const H = innerH + pad.top + pad.bottom;

  // Clamp any stored transform to the valid range for the current dataset
  const scaleMax = Math.max(2, Math.ceil(naturalInnerH / Math.max(innerH, 1)) * 2);
  const rawT = zoomKey ? (_barZooms[zoomKey]?.currentTransform || d3.zoomIdentity) : d3.zoomIdentity;
  const clampedK  = Math.min(rawT.k, scaleMax);
  const clampedTy = Math.max(innerH - naturalInnerH * clampedK, Math.min(0, rawT.y));
  const prevTransform = (rawT.k===1 && rawT.y===0)
    ? d3.zoomIdentity
    : d3.zoomIdentity.translate(0, clampedTy).scale(clampedK);

  const svg=d3.select(svgEl).attr('width',W).attr('height',H);
  svg.selectAll('*').remove();

  const x=d3.scaleLinear().domain([0,d3.max(filteredData,d=>+d[valueKey])*1.12]).range([0,W-pad.left-pad.right]);
  // Full-height band scale — may extend beyond the visible window (innerH)
  const yFull=d3.scaleBand().domain(filteredData.map(d=>d[labelKey])).range([0,naturalInnerH]).padding(0.22);

  // Clip: bars + value labels (bars area width, visible height only)
  const uid=zoomKey||('h'+Math.random().toString(36).slice(2,7));
  const defs=svg.append('defs');
  defs.append('clipPath').attr('id','clip-hb-'+uid)
    .append('rect').attr('x',-4).attr('width',W-pad.left-pad.right+8).attr('height',innerH);
  // Clip: Y-axis labels (left padding region, same visible height)
  defs.append('clipPath').attr('id','clip-hy-'+uid)
    .append('rect').attr('x',-pad.left).attr('width',pad.left+4).attr('height',innerH);

  const g=svg.append('g').attr('transform',`translate(${pad.left},${pad.top})`);

  // X grid — vertical lines across innerH (fixed, not moved by zoom)
  const gGrid=g.append('g').attr('class','d3-grid').attr('clip-path',`url(#clip-hb-${uid})`);
  gGrid.call(d3.axisBottom(x).ticks(5).tickSize(innerH).tickFormat('')).call(a=>a.select('.domain').remove());

  // Bars + value labels (clipped, repositioned by zoom)
  const gBars=g.append('g').attr('clip-path',`url(#clip-hb-${uid})`);

  // Y-axis labels (clipped to visible height, repositioned by zoom)
  const gYAxis=g.append('g').attr('class','d3-axis').attr('clip-path',`url(#clip-hy-${uid})`);

  // X axis — fixed at bottom of visible area, unaffected by zoom
  g.append('g').attr('class','d3-axis')
    .attr('transform',`translate(0,${innerH})`)
    .call(d3.axisBottom(x).ticks(5).tickFormat(pct?v=>v+'%':d3.format(',.0f')));

  // Render / re-render bars and axis with the given band scale.
  // animate=true on initial draw; false during zoom (instant reposition).
  function applyY(y, animate){
    gBars.selectAll('.d3-bar').data(filteredData,d=>d[labelKey])
      .join(
        enter=>enter.append('rect').attr('class','d3-bar')
          .attr('y',d=>y(d[labelKey])).attr('x',0)
          .attr('height',y.bandwidth()).attr('width',0).attr('rx',3)
          .attr('fill',d=>colorFn(d)).attr('fill-opacity',opacOf)
          .style('cursor',onClickBar?'pointer':'default')
          .on('mouseover',(e,d)=>fmtTip&&showTip(e,fmtTip(d)))
          .on('mousemove',moveTip).on('mouseleave',hideTip)
          .on('click',(e,d)=>{ if(onClickBar){e.stopPropagation();onClickBar(d);} })
          .call(el=>el.transition().duration(650).ease(d3.easeCubicOut).attr('width',d=>Math.max(0,x(+d[valueKey])))),
        update=>{
          update.attr('fill-opacity',opacOf).attr('fill',d=>colorFn(d))
            .style('cursor',onClickBar?'pointer':'default')
            .on('mouseover',(e,d)=>fmtTip&&showTip(e,fmtTip(d)))
            .on('mousemove',moveTip).on('mouseleave',hideTip)
            .on('click',(e,d)=>{ if(onClickBar){e.stopPropagation();onClickBar(d);} });
          (animate ? update.transition().duration(500).ease(d3.easeCubicOut) : update)
            .attr('y',d=>y(d[labelKey])).attr('height',y.bandwidth())
            .attr('width',d=>Math.max(0,x(+d[valueKey])));
          return update;
        }
      );

    gBars.selectAll('.blbl').data(filteredData,d=>d[labelKey])
      .join(
        enter=>enter.append('text').attr('class','blbl')
          .attr('y',d=>y(d[labelKey])+y.bandwidth()/2+4).attr('x',d=>x(+d[valueKey])+5)
          .attr('font-size',11).attr('fill','#555').text(d=>fmtVal(+d[valueKey]))
          .attr('fill-opacity',opacOf)
          .style('opacity',0).call(el=>el.transition().delay(400).duration(300).style('opacity',1)),
        update=>{
          update.attr('fill-opacity',opacOf).text(d=>fmtVal(+d[valueKey]));
          (animate ? update.transition().duration(500) : update)
            .attr('y',d=>y(d[labelKey])+y.bandwidth()/2+4).attr('x',d=>x(+d[valueKey])+5);
          return update;
        }
      );

    gYAxis.call(d3.axisLeft(y).tickSize(0)).call(a=>a.select('.domain').remove());
    gYAxis.selectAll('text').attr('dx',-6).attr('font-size',11)
      .style('font-style',italic?'italic':'normal')
      .style('cursor',onClickBar?'pointer':'default')
      .style('font-weight',d=>hasHL&&highlightSet.has(d)?'700':'normal')
      .style('fill',d=>hasHL&&!highlightSet.has(d)?'#aaa':null)
      .on('click',(e,d)=>{ if(onClickBar){ e.stopPropagation(); onClickBar(filteredData.find(f=>f[labelKey]===d)||{[labelKey]:d}); } })
      .text(t=>t.length>27?t.slice(0,26)+'…':t);
  }

  applyY(yFull, true);

  if(zoomKey){
    const chartW = W - pad.left - pad.right;
    // Visual cue: up/down cursor shows the chart is scroll-zoomable
    svg.style('cursor','ns-resize').style('touch-action','none');

    const zoom=d3.zoom()
      // Viewport = inner chart area; world Y extent = full bar list height
      .extent([[0,0],[chartW,innerH]])
      .translateExtent([[0,0],[chartW,naturalInnerH]])
      .scaleExtent([1, scaleMax])
      .on('zoom',(event)=>{
        const t=event.transform;
        // Only use Y component — horizontal drag has no visual effect
        const newY=yFull.copy().range([t.applyY(0),t.applyY(naturalInnerH)]);
        applyY(newY, false);
        if(_barZooms[zoomKey]) _barZooms[zoomKey].currentTransform=t;
      });
    svg.call(zoom);
    _barZooms[zoomKey]={zoom,svgSel:svg,currentTransform:prevTransform};
    if(prevTransform.k!==1||prevTransform.y!==0)
      svg.call(zoom.transform,prevTransform);
  }
}

/* ── D3 vertical (column) bar chart ─────────────────────────────────────── */
function drawVBar(svgEl, wrapEl, data, opts={}){
  if(!svgEl||!wrapEl||!data.length)return;
  const { labelKey='name', valueKey='value', colorFn=()=>'#0077b6',
    fmtVal=v=>d3.format(',.0f')(v), fmtTip, pct=false, italic=false,
    onClickBar=null, highlightSet=null, zoomKey=null } = opts;

  // Filter out zero/empty/missing values to collapse empty space
  const filteredData = data.filter(d => +d[valueKey] > 0);
  if(!filteredData.length)return;

  const hasHL = highlightSet && highlightSet.size > 0;
  const opacOf = d => !hasHL ? 1 : highlightSet.has(d[labelKey]) ? 1 : 0.25;

  const W = wrapEl.clientWidth||700;
  const BW = 36; // bar width per bar
  const pad = { top:8, right:16, bottom:110, left:50 };
  const innerW = Math.max(filteredData.length * (BW + 8), W - pad.left - pad.right);
  const totalW = innerW + pad.left + pad.right;
  const innerH = 260;
  const H = innerH + pad.top + pad.bottom;

  // When chart is wider than container, enable horizontal scroll
  wrapEl.style.overflowX = totalW > W ? 'auto' : '';
  svgEl.style.width = totalW + 'px';   // override CSS width:100% so SVG uses actual coords

  const svg=d3.select(svgEl).attr('width',totalW).attr('height',H);
  svg.selectAll('*').remove();

  const x=d3.scaleBand().domain(filteredData.map(d=>d[labelKey])).range([0,innerW]).padding(0.22);
  const y=d3.scaleLinear().domain([0,d3.max(filteredData,d=>+d[valueKey])*1.12]).range([innerH,0]);
  const g=svg.append('g').attr('transform',`translate(${pad.left},${pad.top})`);

  // grid
  g.append('g').attr('class','d3-grid')
    .call(d3.axisLeft(y).ticks(5).tickSize(-innerW).tickFormat(''))
    .call(a=>a.select('.domain').remove());

  // bars
  g.selectAll('.d3-bar').data(filteredData,d=>d[labelKey])
    .join(
      enter=>enter.append('rect').attr('class','d3-bar')
        .attr('x',d=>x(d[labelKey])).attr('y',innerH)
        .attr('width',x.bandwidth()).attr('height',0).attr('rx',3)
        .attr('fill',d=>colorFn(d)).attr('fill-opacity',opacOf)
        .style('cursor', onClickBar?'pointer':'default')
        .on('mouseover',(e,d)=>fmtTip&&showTip(e,fmtTip(d)))
        .on('mousemove',moveTip).on('mouseleave',hideTip)
        .on('click',(e,d)=>{ if(onClickBar){e.stopPropagation();onClickBar(d);} })
        .call(el=>el.transition().duration(650).ease(d3.easeCubicOut)
          .attr('y',d=>y(+d[valueKey])).attr('height',d=>Math.max(0,innerH-y(+d[valueKey])))),
      update=>update
        .attr('fill-opacity',opacOf)
        .style('cursor', onClickBar?'pointer':'default')
        .on('mouseover',(e,d)=>fmtTip&&showTip(e,fmtTip(d)))
        .on('mousemove',moveTip).on('mouseleave',hideTip)
        .on('click',(e,d)=>{ if(onClickBar){e.stopPropagation();onClickBar(d);} })
        .call(el=>el.transition().duration(500).ease(d3.easeCubicOut)
          .attr('x',d=>x(d[labelKey])).attr('y',d=>y(+d[valueKey]))
          .attr('width',x.bandwidth()).attr('height',d=>Math.max(0,innerH-y(+d[valueKey]))).attr('fill',d=>colorFn(d)))
    );

  // value labels (above bars)
  g.selectAll('.blbl').data(filteredData,d=>d[labelKey])
    .join(
      enter=>enter.append('text').attr('class','blbl')
        .attr('x',d=>x(d[labelKey])+x.bandwidth()/2).attr('y',d=>y(+d[valueKey])-4)
        .attr('text-anchor','middle').attr('font-size',9).attr('fill','#555')
        .text(d=>fmtVal(+d[valueKey])).attr('fill-opacity',opacOf)
        .style('opacity',0).call(el=>el.transition().delay(400).duration(300).style('opacity',1)),
      update=>update.attr('fill-opacity',opacOf).call(el=>el.transition().duration(500)
        .attr('x',d=>x(d[labelKey])+x.bandwidth()/2).attr('y',d=>y(+d[valueKey])-4)
        .text(d=>fmtVal(+d[valueKey])))
    );

  // X axis — rotated tick labels
  const xAxis = g.append('g').attr('class','d3-axis')
    .attr('transform',`translate(0,${innerH})`)
    .call(d3.axisBottom(x).tickSize(0))
    .call(a=>a.select('.domain').remove());
  xAxis.selectAll('text')
    .attr('transform','rotate(-42)')
    .style('text-anchor','end')
    .attr('dx','-0.4em').attr('dy','0.7em')
    .attr('font-size',10)
    .style('font-style',italic?'italic':'normal')
    .style('cursor', onClickBar?'pointer':'default')
    .style('font-weight', d => hasHL && highlightSet.has(d) ? '700' : 'normal')
    .style('fill', d => hasHL && !highlightSet.has(d) ? '#aaa' : null)
    .on('click', (e,d)=>{ if(onClickBar){ e.stopPropagation(); onClickBar(filteredData.find(x=>x[labelKey]===d)||{[labelKey]:d}); } })
    .text(t=>t.length>20?t.slice(0,19)+'…':t);

  // Y axis
  g.append('g').attr('class','d3-axis')
    .call(d3.axisLeft(y).ticks(5).tickFormat(pct?v=>v+'%':d3.format(',.0f')));
}

/* ── D3 lollipop chart ───────────────────────────────────────────────────── */
function drawLollipop(svgEl, wrapEl, data, opts={}){
  if(!svgEl||!wrapEl||!data.length)return;
  const { labelKey='name', valueKey='value', colorFn=()=>'#00b4d8', fmtTip,
    onClickBar=null, highlightSet=null } = opts;

  const hasHL = highlightSet && highlightSet.size > 0;
  const opacOf = d => !hasHL ? 1 : highlightSet.has(d[labelKey]) ? 1 : 0.25;

  const W=wrapEl.clientWidth||700;
  const BH=22, pad={top:8,right:60,bottom:30,left:220};
  const H=data.length*(BH+4)+pad.top+pad.bottom;

  const svg=d3.select(svgEl).attr('width',W).attr('height',H);
  svg.selectAll('*').remove();

  const x=d3.scaleLinear().domain([0,d3.max(data,d=>+d[valueKey])+1]).range([0,W-pad.left-pad.right]);
  const y=d3.scaleBand().domain(data.map(d=>d[labelKey])).range([0,H-pad.top-pad.bottom]).padding(0.3);
  const g=svg.append('g').attr('transform',`translate(${pad.left},${pad.top})`);

  g.selectAll('.lline').data(data).join('line').attr('class','lline')
    .attr('x1',0).attr('x2',d=>x(+d[valueKey]))
    .attr('y1',d=>y(d[labelKey])+y.bandwidth()/2)
    .attr('y2',d=>y(d[labelKey])+y.bandwidth()/2)
    .attr('stroke','#dee2e6').attr('stroke-width',2)
    .attr('stroke-opacity', opacOf);

  g.selectAll('.ldot').data(data).join('circle').attr('class','ldot')
    .attr('cx',d=>x(+d[valueKey])).attr('cy',d=>y(d[labelKey])+y.bandwidth()/2)
    .attr('r',0).attr('fill',d=>colorFn(d)).attr('fill-opacity',opacOf)
    .style('cursor', onClickBar?'pointer':'default')
    .on('mouseover',(e,d)=>fmtTip&&showTip(e,fmtTip(d)))
    .on('mousemove',moveTip).on('mouseleave',hideTip)
    .on('click',(e,d)=>{ if(onClickBar){e.stopPropagation();onClickBar(d);} })
    .call(el=>el.transition().duration(650).ease(d3.easeBackOut).attr('r',7));

  g.selectAll('.llbl').data(data).join('text').attr('class','llbl')
    .attr('x',d=>x(+d[valueKey])+12).attr('y',d=>y(d[labelKey])+y.bandwidth()/2+4)
    .attr('font-size',11).attr('fill','#555').attr('fill-opacity',opacOf).text(d=>d[valueKey]);

  const yAxis = g.append('g').attr('class','d3-axis')
    .call(d3.axisLeft(y).tickSize(0)).call(a=>a.select('.domain').remove());
  yAxis.selectAll('text').attr('dx',-6).attr('font-size',11).style('font-style','italic')
    .style('cursor', onClickBar?'pointer':'default')
    .style('font-weight', d => hasHL && highlightSet.has(d) ? '700' : 'normal')
    .style('fill', d => hasHL && !highlightSet.has(d) ? '#aaa' : null)
    .on('click', (e,d)=>{ if(onClickBar){ e.stopPropagation(); onClickBar(data.find(x=>x[labelKey]===d)||{[labelKey]:d}); } })
    .text(t=>t.length>27?t.slice(0,26)+'…':t);

  g.append('g').attr('class','d3-axis')
    .attr('transform',`translate(0,${H-pad.top-pad.bottom})`)
    .call(d3.axisBottom(x).ticks(5).tickFormat(d3.format('d')));
}

/* ── D3 donut chart ──────────────────────────────────────────────────────── */
function drawDonut(svgEl, wrapEl, legendEl, data, opts={}){
  if(!svgEl||!wrapEl||!data.length)return;
  const {
    labelKey='label', valueKey='value', colorFn=groupColor,
    onClickSlice=null, activeSlice=null,
    onZoomOut=null,
    legendPage=0, legendPageSize=5,
    onLegendPrevPage=null, onLegendNextPage=null,
    onClickLegendItem=null,
  } = opts;
  const W=wrapEl.clientWidth||400;
  const R=Math.min(W,320)/2-10, H=R*2+20;

  const svg=d3.select(svgEl).attr('width',W).attr('height',H);
  svg.selectAll('*').remove();

  const pie=d3.pie().value(d=>+d[valueKey]).sort(null);
  const arc=d3.arc().innerRadius(R*.5).outerRadius(R);
  const arcH=d3.arc().innerRadius(R*.5).outerRadius(R+10);
  const total=d3.sum(data,d=>+d[valueKey]);

  const g=svg.append('g').attr('transform',`translate(${W/2},${H/2})`);

  g.selectAll('path').data(pie(data)).join('path')
    .attr('d',d=> activeSlice&&d.data[labelKey]===activeSlice ? arcH(d) : arc(d))
    .attr('fill',d=>colorFn(d.data[labelKey]))
    .attr('stroke','#fff').attr('stroke-width',2)
    .attr('opacity',d=> activeSlice&&d.data[labelKey]!==activeSlice ? 0.45 : 1)
    .style('cursor','pointer')
    .on('mouseover',function(e,d){
      d3.select(this).transition().duration(120).attr('d',arcH);
      const pct=(d.data[valueKey]/total*100).toFixed(1);
      showTip(e,`<strong>${d.data[labelKey]}</strong>${d3.format(',.0f')(d.data[valueKey])} reads<br>${pct}% of total${onClickSlice?'<br><em style="font-size:.75em">Click to drill down</em>':''}`);
    })
    .on('mousemove',moveTip)
    .on('mouseleave',function(e,d){ d3.select(this).transition().duration(120).attr('d',activeSlice&&d.data[labelKey]===activeSlice?arcH(d):arc(d)); hideTip(); })
    .on('click',(e,d)=>{ if(onClickSlice){ e.stopPropagation(); onClickSlice(d.data); } })
    .transition().duration(700).attrTween('d',function(d){
      const i=d3.interpolate({startAngle:0,endAngle:0},d);
      return t=>arc(i(t));
    });

  // Center back button — shown when a slice is active
  if(activeSlice && onZoomOut){
    g.append('circle').attr('r', R*.42).attr('fill','rgba(255,255,255,0.01)')
      .style('cursor','pointer').on('click', onZoomOut);
    g.append('text').attr('text-anchor','middle').attr('dy','-0.3em')
      .attr('font-size', Math.max(11, R*.13)).attr('fill','#7c6bff').attr('font-weight','600')
      .style('cursor','pointer').text('↩ Back').on('click', onZoomOut);
    g.append('text').attr('text-anchor','middle').attr('dy','1em')
      .attr('font-size', Math.max(9, R*.1)).attr('fill','#999')
      .style('cursor','pointer')
      .text(activeSlice.length>16 ? activeSlice.slice(0,15)+'…' : activeSlice)
      .on('click', onZoomOut);
  }

  if(legendEl){
    const totalPages=Math.ceil(data.length/legendPageSize);
    const page=Math.min(legendPage, Math.max(0,totalPages-1));
    const pageData=data.slice(page*legendPageSize, (page+1)*legendPageSize);
    const esc=s=>String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');

    let html=pageData.map(d=>{
      const lbl=d[labelKey];
      const isActive=activeSlice&&lbl===activeSlice;
      return `<span class="legend-item donut-legend-clickable" data-lk="${esc(lbl)}" `
        +`style="cursor:pointer;${isActive?'font-weight:700;outline:1.5px solid #7c6bff;border-radius:3px;padding:1px 3px':''}">`
        +`<span class="legend-swatch" style="background:${colorFn(lbl)}"></span>${esc(lbl)}</span>`;
    }).join('');

    if(totalPages>1){
      html+=`<div class="legend-pagination">`
        +`<button class="btn btn-ghost btn-xs legend-pg-prev"${page===0?' disabled':''}>‹</button>`
        +`<span class="legend-pg-info">${page+1} / ${totalPages}</span>`
        +`<button class="btn btn-ghost btn-xs legend-pg-next"${page>=totalPages-1?' disabled':''}>›</button>`
        +`</div>`;
    }
    legendEl.innerHTML=html;

    legendEl.querySelectorAll('.donut-legend-clickable').forEach(el=>{
      el.addEventListener('click',()=>{ if(onClickLegendItem) onClickLegendItem(el.dataset.lk); });
    });
    const prevBtn=legendEl.querySelector('.legend-pg-prev');
    const nextBtn=legendEl.querySelector('.legend-pg-next');
    if(prevBtn&&onLegendPrevPage) prevBtn.addEventListener('click', onLegendPrevPage);
    if(nextBtn&&onLegendNextPage) nextBtn.addEventListener('click', onLegendNextPage);
  }
}

/* ── Grouped bar pagination state (keyed by opts.pageKey) ────────────────── */
const _groupedPages = {};

/* ── D3 grouped horizontal bar chart (cross-comparison) ─────────────────────
   Condenses empty rows (taxa with no reads in any group are dropped so there
   are no blank gaps) and paginates long lists so the chart never becomes an
   endless scroll for large classification problems. Pagination is enabled by
   passing a stable `pageKey`; page state is kept internally so callers don't
   need extra Vue wiring. */
function drawGroupedBar(svgEl, wrapEl, legendEl, matrix, groupIds, opts={}){
  if(!svgEl||!wrapEl||!matrix||!matrix.length||!groupIds||!groupIds.length)return;
  const { labelKey='name', valuePrefix='r', valueSuffix='_abundance',
    colorFn=(id,gi)=>d3.schemeTableau10[gi%10],
    labelFn=id=>`#${id}`,
    fmtVal=v=>v.toFixed(2)+'%', fmtTip, pct=false,
    pageKey=null, pageSize=15 } = opts;

  const valOf=(row,id)=>+(row[`${valuePrefix}${id}${valueSuffix}`]||0);

  // Condense: drop rows that are all-zero across every group (no blank space).
  const rows = matrix.filter(row => groupIds.some(id => valOf(row,id) > 0));
  if(!rows.length){
    d3.select(svgEl).selectAll('*').remove();
    if(legendEl) legendEl.innerHTML='';
    return;
  }

  // Pagination: clamp/reset page when the underlying data changes.
  const paginate = !!pageKey && rows.length > pageSize;
  const totalPages = paginate ? Math.ceil(rows.length/pageSize) : 1;
  let page = 0;
  if(paginate){
    const sig = groupIds.join(',')+'|'+rows.length+'|'+(rows[0][labelKey]||'');
    const st = _groupedPages[pageKey];
    page = (st && st.sig===sig) ? Math.min(st.page, totalPages-1) : 0;
    _groupedPages[pageKey] = { page, sig };
  }
  const pageRows = paginate ? rows.slice(page*pageSize, (page+1)*pageSize) : rows;

  const W=wrapEl.clientWidth||700;
  const GH=14, GAP=6;
  const pad={top:8,right:80,bottom:30,left:220};
  const groupH=groupIds.length*GH+GAP;
  const H=pageRows.length*groupH+pad.top+pad.bottom;
  const svg=d3.select(svgEl).attr('width',W).attr('height',H);
  svg.selectAll('*').remove();
  // Scale max derived from the full row set so bar widths stay comparable across pages.
  const maxVal=d3.max(rows,row=>d3.max(groupIds,id=>valOf(row,id)))*1.12||1;
  const x=d3.scaleLinear().domain([0,maxVal]).range([0,W-pad.left-pad.right]);
  const g=svg.append('g').attr('transform',`translate(${pad.left},${pad.top})`);
  g.append('g').attr('class','d3-grid')
    .call(d3.axisBottom(x).ticks(5).tickSize(H-pad.top-pad.bottom).tickFormat(''))
    .call(a=>a.select('.domain').remove());
  pageRows.forEach((row,ri)=>{
    const rowG=g.append('g').attr('transform',`translate(0,${ri*groupH})`);
    groupIds.forEach((id,gi)=>{
      const val=valOf(row,id);
      const color=colorFn(id,gi);
      rowG.append('rect').attr('class','d3-bar')
        .attr('y',gi*GH).attr('x',0).attr('height',GH-2).attr('rx',2)
        .attr('fill',color).attr('fill-opacity',0.82).attr('width',0)
        .on('mouseover',e=>fmtTip&&showTip(e,fmtTip(row,id,val)))
        .on('mousemove',moveTip).on('mouseleave',hideTip)
        .transition().duration(600).ease(d3.easeCubicOut).attr('width',Math.max(0,x(val)));
      if(val>0) rowG.append('text').attr('y',gi*GH+GH/2+3).attr('x',x(val)+4)
        .attr('font-size',9).attr('fill','#555').text(fmtVal(val));
    });
    const nm=row[labelKey]||'';
    g.append('text').attr('x',-6).attr('y',ri*groupH+groupH/2+4)
      .attr('text-anchor','end').attr('font-size',11).attr('fill','#444').style('font-style','italic')
      .text(nm.length>27?nm.slice(0,26)+'…':nm);
  });
  g.append('g').attr('class','d3-axis').attr('transform',`translate(0,${H-pad.top-pad.bottom})`)
    .call(d3.axisBottom(x).ticks(5).tickFormat(pct?v=>v+'%':d3.format(',.2f')));

  if(legendEl){
    let html = groupIds.map((id,gi)=>
      `<span class="legend-item"><span class="legend-swatch" style="background:${colorFn(id,gi)}"></span>${labelFn(id)}</span>`
    ).join('');
    if(paginate){
      const from=page*pageSize+1, to=Math.min((page+1)*pageSize, rows.length);
      html += `<div class="legend-pagination" style="width:100%">`
        +`<button class="btn btn-ghost btn-xs gb-pg-prev"${page===0?' disabled':''}>‹</button>`
        +`<span class="legend-pg-info">${from}–${to} of ${rows.length}</span>`
        +`<button class="btn btn-ghost btn-xs gb-pg-next"${page>=totalPages-1?' disabled':''}>›</button>`
        +`</div>`;
    }
    legendEl.innerHTML=html;
    if(paginate){
      const go=delta=>{
        const st=_groupedPages[pageKey]; if(!st)return;
        st.page=Math.max(0,Math.min(totalPages-1, st.page+delta));
        drawGroupedBar(svgEl, wrapEl, legendEl, matrix, groupIds, opts);
      };
      const prev=legendEl.querySelector('.gb-pg-prev');
      const next=legendEl.querySelector('.gb-pg-next');
      if(prev) prev.addEventListener('click',()=>go(-1));
      if(next) next.addEventListener('click',()=>go(1));
    }
  }
}

/* ── Build color legend (interactive when onToggle is provided) ──────────── */
function buildLegend(el, items, opts={}){
  if(!el)return;
  const { onToggle=null, hiddenSet=null } = opts;
  el.innerHTML=items.map(({label,color,key})=>{
    const k=key!=null?key:label;
    const hidden=hiddenSet&&hiddenSet.has(k);
    const cls=hidden?' legend-item--hidden':'';
    const lk=onToggle?' data-lk="'+k+'"':'';
    const sw='background:'+color+(hidden?';opacity:.22':'');
    const txStyle=hidden?'opacity:.38;text-decoration:line-through':'';
    return '<span class="legend-item'+cls+'"'+lk+'>'
      +'<span class="legend-swatch" style="'+sw+'"></span>'
      +'<span'+(txStyle?' style="'+txStyle+'"':'')+'>'+label+'</span></span>';
  }).join('');
  if(onToggle){
    el.querySelectorAll('[data-lk]').forEach(function(item){
      item.addEventListener('click',function(){ onToggle(item.dataset.lk); });
    });
  }
}

/* ── Generic table sort ──────────────────────────────────────────────────── */
function applySort(state,arr){
  if(!state||!state.key)return arr;
  return [...arr].sort((a,b)=>{
    let av=a[state.key],bv=b[state.key];
    const num=!isNaN(+av)&&!isNaN(+bv);
    if(av==null)av=state.dir==='asc'?Infinity:-Infinity;
    if(bv==null)bv=state.dir==='asc'?Infinity:-Infinity;
    return num ? (state.dir==='asc'?+av-+bv:+bv-+av)
               : (state.dir==='asc'?String(av).localeCompare(String(bv)):String(bv).localeCompare(String(av)));
  });
}

/* ── Bar chart zoom state ────────────────────────────────────────────────── */
const _barZooms = {};
function resetBarZoom(key){
  const z = _barZooms[key];
  if(!z) return;
  z.svgSel.transition().duration(400).call(z.zoom.transform, d3.zoomIdentity);
  if(_barZooms[key]) _barZooms[key].currentTransform = d3.zoomIdentity;
}

/* ── Environmental scatter (e.g. depth vs salinity, colored by group) ──────── */
const ENV_LABELS = { depth:'Depth (m)', salinity:'Salinity', temp:'Temperature (°C)', sst:'Sea-surface temp (°C)', read_count:'Read count' };
function drawScatter(svgEl, wrapEl, data, opts={}){
  const { xKey='salinity', yKey='depth', sizeKey='read_count', colorKey='organism_group',
          invertY=true } = opts;
  if(!svgEl||!wrapEl) return;
  const pts = (data||[]).filter(d => d[xKey]!=null && d[yKey]!=null);
  const W = wrapEl.clientWidth||640, H = opts.height||380;
  const pad = { top:16, right:16, bottom:46, left:60 };
  const svg = d3.select(svgEl).attr('width',W).attr('height',H);
  svg.selectAll('*').remove();
  if(!pts.length){
    svg.append('text').attr('x',W/2).attr('y',H/2).attr('text-anchor','middle')
       .attr('fill','var(--muted)').attr('font-size','13px')
       .text('No points with both '+(ENV_LABELS[xKey]||xKey)+' and '+(ENV_LABELS[yKey]||yKey));
    return;
  }
  const iw = W-pad.left-pad.right, ih = H-pad.top-pad.bottom;
  const xs = d3.extent(pts, d=>+d[xKey]), ys = d3.extent(pts, d=>+d[yKey]);
  const padX = (xs[1]-xs[0]||1)*0.05, padY = (ys[1]-ys[0]||1)*0.05;
  const x = d3.scaleLinear().domain([xs[0]-padX, xs[1]+padX]).range([0,iw]).nice();
  const y = d3.scaleLinear().domain([ys[0]-padY, ys[1]+padY]).range(invertY?[0,ih]:[ih,0]).nice();
  const rExt = d3.extent(pts, d=>+d[sizeKey]||0);
  const r = d3.scaleSqrt().domain([0, rExt[1]||1]).range([2.5, 13]);
  const g = svg.append('g').attr('transform',`translate(${pad.left},${pad.top})`);
  // grid + axes
  g.append('g').attr('class','d3-grid').call(d3.axisLeft(y).tickSize(-iw).tickFormat(''));
  g.append('g').attr('class','d3-grid').attr('transform',`translate(0,${ih})`).call(d3.axisBottom(x).tickSize(-ih).tickFormat(''));
  g.append('g').attr('class','d3-axis').attr('transform',`translate(0,${ih})`).call(d3.axisBottom(x).ticks(6));
  g.append('g').attr('class','d3-axis').call(d3.axisLeft(y).ticks(6));
  g.append('text').attr('x',iw/2).attr('y',ih+38).attr('text-anchor','middle').attr('font-size','12px').attr('fill','var(--muted)').text(ENV_LABELS[xKey]||xKey);
  g.append('text').attr('transform','rotate(-90)').attr('x',-ih/2).attr('y',-44).attr('text-anchor','middle').attr('font-size','12px').attr('fill','var(--muted)').text((ENV_LABELS[yKey]||yKey)+(invertY?' ↓':''));
  g.selectAll('circle').data(pts).join('circle')
    .attr('cx',d=>x(+d[xKey])).attr('cy',d=>y(+d[yKey])).attr('r',d=>r(+d[sizeKey]||0))
    .attr('fill',d=>groupColor(d[colorKey])).attr('fill-opacity',.62)
    .attr('stroke',d=>groupColor(d[colorKey])).attr('stroke-width',.8).style('cursor','pointer')
    .on('mousemove',(e,d)=>showTip(e,`<strong>${d.scientific_name||''}</strong>${d[colorKey]?` · ${d[colorKey]}`:''}<br>${ENV_LABELS[xKey]||xKey}: ${(+d[xKey]).toLocaleString()}<br>${ENV_LABELS[yKey]||yKey}: ${(+d[yKey]).toLocaleString()}<br>${ENV_LABELS[sizeKey]||sizeKey}: ${(+d[sizeKey]||0).toLocaleString()}`))
    .on('mouseleave',hideTip);
}

/* ── Per-species environmental envelope (min–median–max whiskers) ──────────── */
function drawEnvelope(svgEl, wrapEl, data, opts={}){
  const envKey = opts.envKey||'salinity';
  if(!svgEl||!wrapEl) return;
  const rows = (data||[]).filter(d => d[envKey] && d[envKey].n>0);
  const W = wrapEl.clientWidth||640;
  const rowH = 26, pad={ top:14, right:20, bottom:34, left:200 };
  const H = pad.top+pad.bottom+Math.max(1,rows.length)*rowH;
  const svg = d3.select(svgEl).attr('width',W).attr('height',H);
  svg.selectAll('*').remove();
  if(!rows.length){
    svg.append('text').attr('x',W/2).attr('y',H/2).attr('text-anchor','middle').attr('fill','var(--muted)').attr('font-size','13px').text('No '+(ENV_LABELS[envKey]||envKey)+' data for these species');
    return;
  }
  const iw = W-pad.left-pad.right;
  const lo = d3.min(rows,d=>d[envKey].min), hi = d3.max(rows,d=>d[envKey].max);
  const x = d3.scaleLinear().domain([lo,hi]).range([0,iw]).nice();
  const g = svg.append('g').attr('transform',`translate(${pad.left},${pad.top})`);
  g.append('g').attr('class','d3-axis').attr('transform',`translate(0,${rows.length*rowH})`).call(d3.axisBottom(x).ticks(6));
  g.append('text').attr('x',iw/2).attr('y',rows.length*rowH+30).attr('text-anchor','middle').attr('font-size','12px').attr('fill','var(--muted)').text(ENV_LABELS[envKey]||envKey);
  const row = g.selectAll('g.env-row').data(rows).join('g').attr('class','env-row').attr('transform',(d,i)=>`translate(0,${i*rowH})`);
  row.append('text').attr('x',-10).attr('y',rowH/2+4).attr('text-anchor','end').attr('font-size','11px').attr('font-style','italic').attr('fill','var(--text)').text(d=>d.scientific_name.length>30?d.scientific_name.slice(0,29)+'…':d.scientific_name);
  row.append('line').attr('x1',d=>x(d[envKey].min)).attr('x2',d=>x(d[envKey].max)).attr('y1',rowH/2).attr('y2',rowH/2).attr('stroke',d=>groupColor(d.organism_group)).attr('stroke-width',3).attr('stroke-linecap','round').attr('opacity',.5);
  ['min','max'].forEach(k=>row.append('line').attr('x1',d=>x(d[envKey][k])).attr('x2',d=>x(d[envKey][k])).attr('y1',rowH/2-5).attr('y2',rowH/2+5).attr('stroke',d=>groupColor(d.organism_group)).attr('stroke-width',2));
  row.append('circle').attr('cx',d=>x(d[envKey].med)).attr('cy',rowH/2).attr('r',5).attr('fill',d=>groupColor(d.organism_group)).style('cursor','pointer')
    .on('mousemove',(e,d)=>showTip(e,`<strong>${d.scientific_name}</strong><br>${ENV_LABELS[envKey]||envKey} range<br>min ${d[envKey].min} · median ${d[envKey].med} · max ${d[envKey].max}<br>n=${d[envKey].n} detections`))
    .on('mouseleave',hideTip);
}

/* ═══════════════════════════════════════════════════════════════════════════
   Vue App
═══════════════════════════════════════════════════════════════════════════ */
const MBON_APP = createApp({
  setup(){
    const tooltip=ref(null);
    const view=ref('dashboard');
    const dbError=ref(false);

    // ── Sort state ────────────────────────────────────────────────────
    const tableSorts=ref({});
    function sortTable(tbl,key){
      const cur=tableSorts.value[tbl]||{key:null,dir:'desc'};
      tableSorts.value={...tableSorts.value,[tbl]:{key,dir:cur.key===key&&cur.dir==='desc'?'asc':'desc'}};
    }
    function sortArrow(tbl,key){
      const s=tableSorts.value[tbl];
      return(!s||s.key!==key)?'↕':(s.dir==='desc'?'↓':'↑');
    }

    // ── Bar chart orientation toggles (true = horizontal, false = vertical) ──
    const barOrient=ref({
      dashK2: true,
      k2lca: true,
      k2top: true,
      k2cross: true,
      wilderTop: true,
      wilderLolli: true,  // lollipop stays lollipop, toggling has no effect; kept for future use
      groupBar: true,
      mapPopup: true,
      exSpeciesBar: true,
    });
    function toggleBarOrient(key){ barOrient.value={...barOrient.value,[key]:!barOrient.value[key]}; }
    // Dispatch helper: routes to drawHBar or drawVBar based on orientation
    function drawBar(key, svgEl, wrapEl, data, opts){
      const opts2 = { ...opts, zoomKey: key };
      if(barOrient.value[key]) drawHBar(svgEl,wrapEl,data,opts2);
      else drawVBar(svgEl,wrapEl,data,opts2);
    }

    // ── Dashboard ─────────────────────────────────────────────────────
    const stats=ref(null),statsLoading=ref(false);
    const dashDonutSvg=ref(null),dashDonutWrap=ref(null),dashDonutLegend=ref(null);
    const dashK2Svg=ref(null),dashK2Wrap=ref(null);
    // Donut drill-down
    const dashDonutAllData=ref([]);
    const dashDonutSelected=ref(null);
    const dashDonutDrillData=ref([]);
    const dashDonutDrillLoading=ref(false);
    const dashDonutSearch=ref('');
    const dashDonutLegendPage=ref(0);
    // K2 bar drill-down
    const dashK2AllData=ref([]);
    const dashK2Search=ref('');
    const dashK2DrillName=ref('');
    const dashK2DrillData=ref(null);
    const dashK2DrillLoading=ref(false);

    async function loadStats(){
      statsLoading.value=true;
      try{ const r=await axios.get(`${API}/stats`); stats.value=r.data; dbError.value=false;
        await nextTick(); loadDashCharts(); }
      catch{ dbError.value=true; }
      finally{ statsLoading.value=false; }
    }
    function redrawDashDonut(){
      if(!dashDonutAllData.value.length)return;
      // Apply regex search filter — find best match and auto-select it
      let displayData = dashDonutAllData.value;
      const q = dashDonutSearch.value.trim();
      if(q){
        let rx;
        try{ rx = new RegExp(q,'i'); }catch{ rx = null; }
        if(rx) displayData = dashDonutAllData.value.filter(d=>(d.organism_group||'').match(rx));
        else   displayData = dashDonutAllData.value.filter(d=>(d.organism_group||'').toLowerCase().includes(q.toLowerCase()));
        // Auto-select best hit if there is exactly one match or the selected is gone
        if(displayData.length===1){
          if(dashDonutSelected.value !== displayData[0].organism_group){
            dashDonutSelected.value = displayData[0].organism_group;
            dashDonutLegendPage.value = 0;
            _fetchDonutDrill(dashDonutSelected.value);
          }
        } else if(displayData.length>0 && dashDonutSelected.value && !displayData.find(d=>d.organism_group===dashDonutSelected.value)){
          // Picked slice no longer in filtered set — auto-select first
          dashDonutSelected.value = displayData[0].organism_group;
          dashDonutLegendPage.value = 0;
          _fetchDonutDrill(dashDonutSelected.value);
        }
      }
      if(!displayData.length) displayData = dashDonutAllData.value; // fallback: no match → show all
      drawDonut(dashDonutSvg.value,dashDonutWrap.value,dashDonutLegend.value,
        displayData,{
          labelKey:'organism_group',valueKey:'total_reads',
          onClickSlice:d=>dashDonutClick(d.organism_group),
          onClickLegendItem:lbl=>dashDonutClick(lbl),
          activeSlice:dashDonutSelected.value,
          onZoomOut:()=>{ dashDonutSelected.value=null; dashDonutDrillData.value=[]; redrawDashDonut(); },
          legendPage:dashDonutLegendPage.value,
          onLegendPrevPage:()=>{ dashDonutLegendPage.value=Math.max(0,dashDonutLegendPage.value-1); redrawDashDonut(); },
          onLegendNextPage:()=>{ const max=Math.ceil(displayData.length/5)-1; dashDonutLegendPage.value=Math.min(max,dashDonutLegendPage.value+1); redrawDashDonut(); },
        });
    }
    async function _fetchDonutDrill(group){
      dashDonutDrillLoading.value=true; dashDonutDrillData.value=[];
      try{
        const r=await axios.get(`${API}/wilder/top-taxa?organism_group=${encodeURIComponent(group)}&limit=12`);
        dashDonutDrillData.value=r.data;
      }catch{}
      finally{dashDonutDrillLoading.value=false;}
    }
    async function dashDonutClick(group){
      if(dashDonutSelected.value===group){ dashDonutSelected.value=null; dashDonutDrillData.value=[]; redrawDashDonut(); return; }
      dashDonutSelected.value=group;
      dashDonutLegendPage.value=0;
      redrawDashDonut();
      await _fetchDonutDrill(group);
    }
    async function dashDonutNavGroup(group){
      await nav('wilder');
      wilderTab.value='profiles'; wilderTopGroup.value=group;
      await nextTick(); loadWilderTopTaxa();
    }
    function drawDashK2Chart(){
      const q=dashK2Search.value.toLowerCase();
      const data=q?dashK2AllData.value.filter(d=>(d.name||'').toLowerCase().includes(q)):dashK2AllData.value;
      if(!data.length)return;
      drawBar('dashK2', dashK2Svg.value,dashK2Wrap.value,data,{
        labelKey:'name',valueKey:'avg_abundance',colorFn:()=>RANK_COLORS['S'],
        fmtVal:v=>v.toFixed(2)+'%',pct:true,
        fmtTip:d=>`<strong>${d.name}</strong>Avg abundance: ${Number(d.avg_abundance).toFixed(3)}%<br><em style="font-size:.75em">Click to see cross-report data</em>`,
        onClickBar:d=>dashK2Click(d.name),
        highlightSet:dashK2DrillName.value?new Set([dashK2DrillName.value]):null,
      });
    }
    async function dashK2Click(name){
      if(dashK2DrillName.value===name){ dashK2DrillName.value=''; dashK2DrillData.value=null; return; }
      dashK2DrillName.value=name; dashK2DrillLoading.value=true; dashK2DrillData.value=null;
      try{
        const r=await axios.get(`${API}/kraken2/taxon-compare?name=${encodeURIComponent(name)}`);
        dashK2DrillData.value=r.data;
      }catch{ dashK2DrillData.value=[]; }
      finally{dashK2DrillLoading.value=false;}
    }
    async function dashK2NavToK2(name){
      k2CrossName.value=name; k2Tab.value='browser';
      await nav('kraken2'); await nextTick(); loadK2CrossCompare(name);
    }
    async function loadDashCharts(){
      try{
        const g=await axios.get(`${API}/wilder/organism-groups`);
        dashDonutAllData.value=g.data;
        if(g.data.length) redrawDashDonut();
      }catch{}
      try{
        const k=await axios.get(`${API}/kraken2/top-taxa?limit=20&rank=S`);
        dashK2AllData.value=k.data;
        drawDashK2Chart();
      }catch{}
    }

    // ── Collections ───────────────────────────────────────────────────
    const collections=ref([]),collectionsLoading=ref(false),collectionSearch=ref(''),collectionProjectFilter=ref('');
    const selectedCollection=ref(null),projects=ref([]);
    const filteredCollections=computed(()=>{
      const s=collectionSearch.value.toLowerCase();
      return applySort(tableSorts.value['collections'],collections.value).filter(c=>
        (!s||(c.collection_name||'').toLowerCase().includes(s))&&
        (!collectionProjectFilter.value||c.project_id==collectionProjectFilter.value));
    });
    async function loadCollections(){ collectionsLoading.value=true; try{const r=await axios.get(`${API}/collections?limit=200`);collections.value=r.data;}catch{} finally{collectionsLoading.value=false;} }
    async function loadProjects(){ try{const r=await axios.get(`${API}/projects`);projects.value=r.data;}catch{} }

    // ── Runs ──────────────────────────────────────────────────────────
    const runs=ref([]),runsLoading=ref(false),runSearch=ref(''),runCollectionFilter=ref('');
    const selectedRun=ref(null);
    const filteredRuns=computed(()=>{
      const s=runSearch.value.toLowerCase();
      return applySort(tableSorts.value['runs'],runs.value).filter(r=>
        (!s||(r.directory||'').toLowerCase().includes(s)||(r.comments||'').toLowerCase().includes(s))&&
        (!runCollectionFilter.value||r.collection_id==runCollectionFilter.value));
    });
    async function loadRuns(){ runsLoading.value=true; try{const r=await axios.get(`${API}/runs?limit=500`);runs.value=r.data;}catch{} finally{runsLoading.value=false;} }

    // ── Samples ───────────────────────────────────────────────────────
    const samples=ref([]),samplesLoading=ref(false),sampleSearch=ref(''),sampleTypeFilter=ref(''),sampleCollectionFilter=ref('');
    const selectedSample=ref(null),selectedSampleSummary=ref(null);
    const sortedSamples=computed(()=>applySort(tableSorts.value['samples'],samples.value));
    async function loadSamples(){
      samplesLoading.value=true; selectedSample.value=null;
      const p=new URLSearchParams({limit:500});
      if(sampleSearch.value)p.set('search',sampleSearch.value);
      if(sampleTypeFilter.value)p.set('sample_type',sampleTypeFilter.value);
      if(sampleCollectionFilter.value)p.set('collection_id',sampleCollectionFilter.value);
      try{const r=await axios.get(`${API}/samples?${p}`);samples.value=r.data;}catch{}
      finally{samplesLoading.value=false;}
    }
    async function openSampleDetail(s){
      selectedSample.value=s; selectedSampleSummary.value=null;
      try{const r=await axios.get(`${API}/samples/${s.sample_id}/summary`);
        selectedSampleSummary.value=r.data&&Object.keys(r.data).length?r.data:null;}catch{}
    }

    // ── Kraken2 ───────────────────────────────────────────────────────
    const k2Tab=ref('reports'),k2Reports=ref([]),k2Loading=ref(false);
    const k2Lcas=ref([]),k2LcaLoading=ref(false),k2SelectedReport=ref(''),k2RankFilter=ref('S'),k2MinAbundance=ref('');
    const k2Sort=ref('abundance-desc'),k2TopN=ref(20);
    const k2AllowedRanks=ref(new Set(['D','K','P','C','O','F','G','S','U']));
    const k2ChartSearch=ref('');
    const k2LoadedRanks=computed(()=>[...new Set(k2Lcas.value.map(d=>d.rank))].sort());
    const k2RankOptions=ref([]),k2TopRankOptions=ref([]);
    // Robust dropdown options: never blank when data exists. Union the API-provided
    // rank list with ranks actually present in the loaded rows, sorted Species-first
    // (sortRanks uses PREFERRED_RANKS = S,G,F,O,C,P,K,D,U).
    const k2RankSelectOptions=computed(()=>sortRanks([...(k2RankOptions.value||[]), ...k2Lcas.value.map(d=>d.rank)]));
    const k2ChartSvg=ref(null),k2ChartWrap=ref(null),k2Legend=ref(null);
    const k2TopTaxa=ref([]),k2TopLoading=ref(false),k2TopRank=ref('S'),k2TopSort=ref('avg-desc');
    const k2TopSvg=ref(null),k2TopWrap=ref(null),k2TopLegend=ref(null);
    const k2HiddenRanks=ref(new Set());
    const k2TopHiddenRanks=ref(new Set());
    const k2NameFilter=ref(''),k2NameTags=ref([]);
    const k2TopNameFilter=ref(''),k2TopNameTags=ref([]);
    // Search, sunburst, compare
    const k2ReportSearch=ref('');
    // Infinite sunburst panels — each panel is a plain reactive object in the array
    const k2SbPanels=ref([{id:1,report:'',data:[],loading:false,search:'',reportSearch:'',mainRanks:false,unclassReads:0}]);
    let _sbNextId=2;
    // Plain JS objects keyed by panel.id — populated by Vue function refs in template
    const sbSvgEls={},sbWrapEls={},sbLegendEls={},sbListEls={},_sbStates={};
    function addSbPanel(){ k2SbPanels.value.push({id:_sbNextId++,report:'',data:[],loading:false,search:'',reportSearch:'',mainRanks:false,unclassReads:0}); }
    function removeSbPanel(id){ k2SbPanels.value=k2SbPanels.value.filter(p=>p.id!==id); delete sbSvgEls[id]; delete sbWrapEls[id]; delete sbLegendEls[id]; delete sbListEls[id]; delete _sbStates[id]; }
    function resetSbPanel(id){ const st=_sbStates[id]; if(st) sbFocusOn(st.root,st); }
    function applySbFilters(panel){ const st=_sbStates[panel.id]; if(st) applyK2SunburstFilters(st); }
    // LCA highlight selection (multi-click table rows → filter chart)
    const k2LcaHighlighted=ref(new Set());
    // Cross-report taxon comparison panel
    const k2CrossName=ref(''),k2CrossData=ref(null),k2CrossLoading=ref(false);
    const k2CrossSvg=ref(null),k2CrossWrap=ref(null);
    const k2CompareSelected=ref([]),k2CompareRank=ref('S'),k2CompareTop=ref(20);
    const k2CompareData=ref(null),k2CompareLoading=ref(false);
    const k2CompareSvg=ref(null),k2CompareWrap=ref(null),k2CompareLegend=ref(null);
    const k2ComparePlotFilter=ref(''),k2CompareTableSearch=ref(''),k2CompareTableShowAll=ref(false);
    // Searchable report picker (LCA Abundance Profile tab) — mirrors the Reports-tab search box
    const k2ReportPickerSearch=ref('');
    const filteredK2ReportsForPicker=computed(()=>{
      const s=k2ReportPickerSearch.value.toLowerCase();
      if(!s) return k2Reports.value;
      return k2Reports.value.filter(r=>
        (r.database||'').toLowerCase().includes(s)||
        (r.report_location||'').toLowerCase().includes(s)||
        (r.mbon_samp_alias||'').toLowerCase().includes(s)||
        String(r.id).includes(s));
    });
    // Per-panel report search text, used by the Sunburst panels (each panel gets its own filter)
    function filteredK2ReportsForPanel(panel){
      const s=(panel.reportSearch||'').toLowerCase();
      if(!s) return k2Reports.value;
      return k2Reports.value.filter(r=>
        (r.database||'').toLowerCase().includes(s)||
        (r.report_location||'').toLowerCase().includes(s)||
        (r.mbon_samp_alias||'').toLowerCase().includes(s)||
        String(r.id).includes(s));
    }

    const sortedK2LCAs=computed(()=>{
      let d=k2Lcas.value;
      if(k2NameTags.value.length){
        const tags=k2NameTags.value.map(t=>t.toLowerCase());
        d=d.filter(x=>tags.some(t=>(x.name||'').toLowerCase().includes(t)));
      }
      return applySort(tableSorts.value['k2lcas']||{key:'abundance',dir:'desc'},d);
    });
    const sortedK2TopTaxa=computed(()=>{
      let d=k2TopTaxa.value;
      if(k2TopNameTags.value.length){
        const tags=k2TopNameTags.value.map(t=>t.toLowerCase());
        d=d.filter(x=>tags.some(t=>(x.name||'').toLowerCase().includes(t)));
      }
      return applySort(tableSorts.value['k2top']||{key:'avg_abundance',dir:'desc'},d);
    });
    const filteredK2ReportsForCompare=computed(()=>{
      const s=k2CompareTableSearch.value.toLowerCase();
      if(!s)return k2Reports.value;
      return k2Reports.value.filter(r=>
        (r.database||'').toLowerCase().includes(s)||
        (r.report_location||'').toLowerCase().includes(s)||
        (r.mbon_samp_alias||'').toLowerCase().includes(s)||
        (r.station||'').toLowerCase().includes(s)||
        (r.cruise_number||'').toLowerCase().includes(s)||
        String(r.id).includes(s)
      );
    });
    const filteredWilderCompareSamples=computed(()=>{
      const s=wilderCompareTableSearch.value.toLowerCase();
      if(!s)return wilderCompareSamples.value;
      return wilderCompareSamples.value.filter(m=>
        (m.mbon_samp_alias||'').toLowerCase().includes(s)||
        (m.cruise_number||'').toLowerCase().includes(s)||
        (m.station||'').toLowerCase().includes(s)||
        String(m.mbon_data_id).includes(s)
      );
    });
    const wilderCompareCruises=computed(()=>[...new Set(wilderCompareSamples.value.map(s=>s.cruise_number).filter(Boolean))].sort());
    const wilderCompareStationList=computed(()=>[...new Set(wilderCompareSamples.value.map(s=>s.station).filter(Boolean))].sort());
    function wilderSelectAll(){ wilderCompareSelected.value=[...new Set([...wilderCompareSelected.value,...filteredWilderCompareSamples.value.map(s=>s.mbon_data_id)])]; }
    function toggleWilderByCruise(cruise){
      const ids=wilderCompareSamples.value.filter(s=>s.cruise_number===cruise).map(s=>s.mbon_data_id);
      const allSelected=ids.every(id=>wilderCompareSelected.value.includes(id));
      if(allSelected) wilderCompareSelected.value=wilderCompareSelected.value.filter(id=>!ids.includes(id));
      else wilderCompareSelected.value=[...new Set([...wilderCompareSelected.value,...ids])];
    }
    function toggleWilderByStation(station){
      const ids=wilderCompareSamples.value.filter(s=>s.station===station).map(s=>s.mbon_data_id);
      const allSelected=ids.every(id=>wilderCompareSelected.value.includes(id));
      if(allSelected) wilderCompareSelected.value=wilderCompareSelected.value.filter(id=>!ids.includes(id));
      else wilderCompareSelected.value=[...new Set([...wilderCompareSelected.value,...ids])];
    }

    async function loadK2Reports(){
      k2Loading.value=true;
      try{
        const p=new URLSearchParams({limit:200});
        if(k2ReportSearch.value)p.set('search',k2ReportSearch.value);
        const r=await axios.get(`${API}/kraken2/reports?${p}`);
        k2Reports.value=r.data;
      }catch{}
      finally{k2Loading.value=false;}
    }
    async function selectK2Report(r){ k2SelectedReport.value=r.id; k2Tab.value='browser'; }
    async function loadK2RankOptions(reportId){
      const p=new URLSearchParams();
      if(reportId)p.set('report_id',reportId);
      try{
        const r=await axios.get(`${API}/kraken2/ranks?${p}`);
        const opts=sortRanks((r.data||[]).map(x=>x.rank));
        if(reportId){
          const fallback=k2TopRankOptions.value.length?k2TopRankOptions.value:k2LoadedRanks.value;
          const nextOpts=opts.length?opts:sortRanks(fallback);
          k2RankOptions.value=nextOpts;
          k2RankFilter.value=chooseRank(nextOpts,k2RankFilter.value);
        }else{
          k2TopRankOptions.value=opts;
          k2TopRank.value=chooseRank(opts,k2TopRank.value);
          if(!k2SelectedReport.value || !k2RankOptions.value.length){
            k2RankOptions.value=opts;
            k2RankFilter.value=chooseRank(opts,k2RankFilter.value);
          }
          mapK2RankOptions.value=opts;
          mapK2Rank.value=chooseRank(opts,mapK2Rank.value);
        }
      }catch{}
    }
    async function loadK2LCAs(){
      if(!k2SelectedReport.value)return;
      k2LcaLoading.value=true;
      const p=new URLSearchParams({report_id:k2SelectedReport.value});
      if(k2RankFilter.value)p.set('rank',k2RankFilter.value);
      if(k2MinAbundance.value)p.set('min_abundance',k2MinAbundance.value);
      try{const r=await axios.get(`${API}/kraken2/lcas?${p}`);k2Lcas.value=r.data;}catch{}
      finally{k2LcaLoading.value=false;}
      k2AllowedRanks.value=new Set(k2LoadedRanks.value.filter(r=>MAIN_RANKS.has(r)));
      k2HiddenRanks.value=new Set();
      await nextTick();
      if(k2Lcas.value.length) drawK2Chart();
    }
    function k2SortData(){
      let d=[...k2Lcas.value];
      // Filter by allowed ranks (client-side; hides non-standard ranks by default)
      if(k2AllowedRanks.value.size>0) d=d.filter(x=>k2AllowedRanks.value.has(x.rank));
      // Filter out legend-hidden ranks
      if(k2HiddenRanks.value.size>0) d=d.filter(x=>!k2HiddenRanks.value.has(x.rank));
      // Quick chart search
      if(k2ChartSearch.value){ const q=k2ChartSearch.value.toLowerCase(); d=d.filter(x=>(x.name||'').toLowerCase().includes(q)); }
      if(k2NameTags.value.length){
        const tags=k2NameTags.value.map(t=>t.toLowerCase());
        d=d.filter(x=>tags.some(t=>(x.name||'').toLowerCase().includes(t)));
      }
      return k2Sort.value==='abundance-asc'?d.sort((a,b)=>+a.abundance-+b.abundance)
           : k2Sort.value==='count-desc'?d.sort((a,b)=>+b.count-+a.count)
           : k2Sort.value==='name'?d.sort((a,b)=>a.name.localeCompare(b.name))
           : d.sort((a,b)=>+b.abundance-+a.abundance);
    }
    function toggleK2AllowedRank(r){ const s=new Set(k2AllowedRanks.value); if(s.has(r))s.delete(r); else s.add(r); k2AllowedRanks.value=s; drawK2Chart(); }
    function k2ShowStdRanks(){ k2AllowedRanks.value=new Set(k2LoadedRanks.value.filter(r=>MAIN_RANKS.has(r))); drawK2Chart(); }
    function k2ShowAllRanks(){ k2AllowedRanks.value=new Set(k2LoadedRanks.value); drawK2Chart(); }
    function drawK2Chart(){
      let data=k2SortData().slice(0,k2TopN.value);
      // If rows are highlighted, filter chart to only those
      if(k2LcaHighlighted.value.size>0){
        const sel=k2SortData().filter(d=>k2LcaHighlighted.value.has(d.name));
        if(sel.length)data=sel;
      }
      drawBar('k2lca', k2ChartSvg.value,k2ChartWrap.value,data,{
        labelKey:'name',valueKey:'abundance',colorFn:d=>rankColor(d.rank),
        fmtVal:v=>v.toFixed(2)+'%',pct:true,
        fmtTip:d=>`<strong>${d.name}</strong>Rank: ${rankLabel(d.rank)}<br>Abundance: ${Number(d.abundance).toFixed(3)}%<br>Count: ${Number(d.count).toLocaleString()}`,
      });
      const ranks=[...new Set(data.map(d=>d.rank))];
      // Legend: derive from the full allowed data so hidden ranks stay visible (can be re-toggled)
      const legendRanks=[...new Set(k2Lcas.value.filter(x=>k2AllowedRanks.value.size>0?k2AllowedRanks.value.has(x.rank):true).map(d=>d.rank))];
      buildLegend(k2Legend.value,legendRanks.map(r=>({label:rankLabel(r),color:rankColor(r),key:r})),{
        onToggle:r=>toggleK2HiddenRank(r), hiddenSet:k2HiddenRanks.value });
    }
    function toggleK2HiddenRank(r){ const s=new Set(k2HiddenRanks.value); if(s.has(r))s.delete(r); else s.add(r); k2HiddenRanks.value=s; drawK2Chart(); }
    async function loadK2TopTaxa(){
      k2TopLoading.value=true;
      const p=new URLSearchParams({limit:30});
      if(k2TopRank.value)p.set('rank',k2TopRank.value);
      try{const r=await axios.get(`${API}/kraken2/top-taxa?${p}`);k2TopTaxa.value=r.data;}catch{}
      finally{k2TopLoading.value=false;}
      k2TopHiddenRanks.value=new Set();
      await nextTick();
      if(k2TopTaxa.value.length) drawK2TopChart();
    }
    function drawK2TopChart(){
      let d=[...k2TopTaxa.value];
      if(k2TopHiddenRanks.value.size>0) d=d.filter(x=>!k2TopHiddenRanks.value.has(x.rank));
      if(k2TopNameTags.value.length){
        const tags=k2TopNameTags.value.map(t=>t.toLowerCase());
        d=d.filter(x=>tags.some(t=>(x.name||'').toLowerCase().includes(t)));
      }
      const data=k2TopSort.value==='count-desc'?d.sort((a,b)=>+b.total_count-+a.total_count)
               : k2TopSort.value==='name'?d.sort((a,b)=>a.name.localeCompare(b.name))
               : d.sort((a,b)=>+b.avg_abundance-+a.avg_abundance);
      drawBar('k2top', k2TopSvg.value,k2TopWrap.value,data,{
        labelKey:'name',valueKey:'avg_abundance',colorFn:d=>rankColor(d.rank),
        fmtVal:v=>v.toFixed(3)+'%',pct:true,
        fmtTip:d=>`<strong>${d.name}</strong>Rank: ${rankLabel(d.rank)}<br>Avg abundance: ${Number(d.avg_abundance).toFixed(3)}%<br>Total count: ${Number(d.total_count).toLocaleString()}`,
      });
      const legendRanks=[...new Set(k2TopTaxa.value.map(d=>d.rank))];
      buildLegend(k2TopLegend.value, legendRanks.map(r=>({label:rankLabel(r),color:rankColor(r),key:r})),{
        onToggle:r=>toggleK2TopHiddenRank(r), hiddenSet:k2TopHiddenRanks.value });
    }
    function toggleK2TopHiddenRank(r){ const s=new Set(k2TopHiddenRanks.value); if(s.has(r))s.delete(r); else s.add(r); k2TopHiddenRanks.value=s; drawK2TopChart(); }

    // ── MBON ──────────────────────────────────────────────────────────
    const mbonSamples=ref([]),mbonLoading=ref(false),mbonSearch=ref(''),mbonCruiseFilter=ref(''),mbonStationFilter=ref('');
    const mbonCruises=ref([]),mbonStations=ref([]),selectedMbon=ref(null);
    const sortedMbon=computed(()=>applySort(tableSorts.value['mbon'],mbonSamples.value));
    async function loadMbon(){
      mbonLoading.value=true; selectedMbon.value=null;
      const p=new URLSearchParams({limit:200});
      if(mbonSearch.value)p.set('search',mbonSearch.value);
      if(mbonCruiseFilter.value)p.set('cruise_number',mbonCruiseFilter.value);
      if(mbonStationFilter.value)p.set('station',mbonStationFilter.value);
      try{const r=await axios.get(`${API}/mbon/samples?${p}`);mbonSamples.value=r.data;}catch{}
      finally{mbonLoading.value=false;}
    }
    async function loadMbonFilters(){
      try{const [c,s]=await Promise.all([axios.get(`${API}/mbon/cruises`),axios.get(`${API}/mbon/stations`)]);
        mbonCruises.value=c.data;mbonStations.value=s.data;}catch{}
    }

    // ── Wilderlab ─────────────────────────────────────────────────────
    const wilderTab=ref('profiles');
    const wilderResults=ref([]),wilderLoading=ref(false);
    const wilderJobs=ref([]),wilderJobsLoading=ref(false);
    const wilderGroups=ref([]),wilderGroupsLoading=ref(false);
    const wilderTopTaxa=ref([]),wilderTopLoading=ref(false);
    const wilderJobFilter=ref(''),wilderGroupFilter=ref(''),wilderMinReads=ref('');
    const wilderSampleFilter=ref(''),wilderResSearch=ref('');
    const wilderTopGroup=ref(''),wilderTopJob=ref(''),wilderTopSample=ref('');
    const wSort=ref('reads-desc'),wTopN=ref(20),groupSort=ref('reads');
    const wNameFilter=ref(''),wNameTags=ref([]);
    const wAllowedRanks=ref(new Set(['species', "genus", "family", "subfamily"]));
    const wChartSearch=ref('');
    const wLoadedRanks=computed(()=>[...new Set(wilderTopTaxa.value.map(d=>d.taxonomic_rank).filter(Boolean))].sort());

    const wilderChartSvg=ref(null),wilderChartWrap=ref(null),wilderLegend=ref(null);
    const wilderLolliSvg=ref(null),wilderLolliWrap=ref(null);
    const groupBarSvg=ref(null),groupBarWrap=ref(null),groupBarLegend=ref(null);
    const wilderHiddenGroups=ref(new Set());
    const groupBarHiddenGroups=ref(new Set());
    const groupDonutSvg=ref(null),groupDonutWrap=ref(null),groupDonutLegend=ref(null);
    // Wilder sunburst panels (multi-panel system like K2)
    const wilderSbSamples=ref([]);  // [{mbon_data_id, mbon_samp_alias, wilder_job_id}]
    const wilderSbPanels=ref([{id:1,sample:'',group:'',data:[],loading:false,search:'',sampleSearch:'',_taxa:[]}]);
    let _wSbNextId=2;
    const wSbSvgEls={},wSbWrapEls={},wSbLegendEls={},wSbListEls={},_wSbStates={};
    // Searchable sample pickers — filter the (potentially long) sample dropdowns by alias/ID
    function filterWilderSamples(list, text){
      const s=(text||'').toLowerCase();
      if(!s) return list;
      return list.filter(m=>
        (m.mbon_samp_alias||'').toLowerCase().includes(s)||
        (m.wilder_job_id||'').toString().toLowerCase().includes(s)||
        String(m.mbon_data_id).includes(s));
    }
    const wilderProfileSampleSearch=ref('');
    const filteredWilderSbSamplesForProfile=computed(()=>filterWilderSamples(wilderSbSamples.value,wilderProfileSampleSearch.value));
    const wilderResultsSampleSearch=ref('');
    const filteredWilderSbSamplesForResults=computed(()=>filterWilderSamples(wilderSbSamples.value,wilderResultsSampleSearch.value));
    function filteredWilderSbSamplesForPanel(panel){ return filterWilderSamples(wilderSbSamples.value,panel.sampleSearch); }

    const sortedWilderResults=computed(()=>applySort(tableSorts.value['wres']||{key:'read_count',dir:'desc'},wilderResults.value));
    const sortedWilderTopTaxa=computed(()=>{
      let d=wilderTopTaxa.value;
      if(wNameTags.value.length){
        const tags=wNameTags.value.map(t=>t.toLowerCase());
        d=d.filter(x=>tags.some(t=>(x.scientific_name||'').toLowerCase().includes(t)||(x.common_name||'').toLowerCase().includes(t)));
      }
      return applySort(tableSorts.value['wtaxa']||{key:'total_reads',dir:'desc'},d);
    });
    const sortedWilderJobs=computed(()=>applySort(tableSorts.value['wjobs']||{key:'total_reads',dir:'desc'},wilderJobs.value));
    const sortedWilderGroups=computed(()=>applySort(tableSorts.value['wgroups']||{key:'total_reads',dir:'desc'},wilderGroups.value));

    async function loadWilderResults(){
      wilderLoading.value=true;
      const p=new URLSearchParams({limit:200});
      if(wilderJobFilter.value)p.set('wilder_job_id',wilderJobFilter.value);
      if(wilderGroupFilter.value)p.set('organism_group',wilderGroupFilter.value);
      if(wilderMinReads.value)p.set('min_reads',wilderMinReads.value);
      if(wilderSampleFilter.value)p.set('mbon_data_id',wilderSampleFilter.value);
      if(wilderResSearch.value)p.set('search',wilderResSearch.value);
      try{const r=await axios.get(`${API}/wilder/results?${p}`);wilderResults.value=r.data;}catch{}
      finally{wilderLoading.value=false;}
    }
    async function loadWilderJobs(){
      wilderJobsLoading.value=true;
      try{const r=await axios.get(`${API}/wilder/jobs`);wilderJobs.value=r.data;}catch{}
      finally{wilderJobsLoading.value=false;}
    }
    async function loadWilderGroups(){
      wilderGroupsLoading.value=true;
      try{
        const p=new URLSearchParams();
        if(wilderGroupsJobFilter.value)p.set('job',wilderGroupsJobFilter.value);
        const r=await axios.get(`${API}/wilder/organism-groups?${p}`);
        wilderGroups.value=r.data;
      }catch{}
      finally{wilderGroupsLoading.value=false;}
      if(wilderTab.value==='groups' && wilderGroups.value.length){
        await nextTick();
        drawGroupChart();
        drawDonut(groupDonutSvg.value,groupDonutWrap.value,groupDonutLegend.value,
          wilderGroups.value.slice(0,14),{labelKey:'organism_group',valueKey:'total_reads',colorFn:groupColor});
      }
    }
    async function loadWilderTopTaxa(){
      wilderTopLoading.value=true;
      const p=new URLSearchParams({limit:60});
      if(wilderTopGroup.value)p.set('organism_group',wilderTopGroup.value);
      if(wilderTopJob.value)p.set('wilder_job_id',wilderTopJob.value);
      if(wilderTopSample.value)p.set('mbon_data_id',wilderTopSample.value);
      try{
        const r=await axios.get(`${API}/wilder/top-taxa?${p}`);
        wilderTopTaxa.value=r.data;
      }catch{}
      finally{wilderTopLoading.value=false;}
      // Reset rank filter to species only on each fresh load
      wAllowedRanks.value=new Set(['species', "genus", "family", "subfamily"]);
      wilderHiddenGroups.value=new Set();
      await nextTick();
      if(wilderTopTaxa.value.length){ drawWilderChart(); drawWilderLolli(); }
    }
    function wilderSortData(){
      let d=[...wilderTopTaxa.value];
      // Filter by allowed ranks (client-side; defaults to species only)
      if(wAllowedRanks.value.size>0) d=d.filter(x=>wAllowedRanks.value.has((x.taxonomic_rank||'').toLowerCase()));
      // Filter out legend-hidden organism groups
      if(wilderHiddenGroups.value.size>0) d=d.filter(x=>!wilderHiddenGroups.value.has(x.organism_group));
      // Quick chart search
      if(wChartSearch.value){ const q=wChartSearch.value.toLowerCase(); d=d.filter(x=>(x.scientific_name||'').toLowerCase().includes(q)||(x.common_name||'').toLowerCase().includes(q)); }
      if(wNameTags.value.length){
        const tags=wNameTags.value.map(t=>t.toLowerCase());
        d=d.filter(x=>tags.some(t=>(x.scientific_name||'').toLowerCase().includes(t)||(x.common_name||'').toLowerCase().includes(t)));
      }
      return wSort.value==='reads-asc'?d.sort((a,b)=>+a.total_reads-+b.total_reads)
           : wSort.value==='samples-desc'?d.sort((a,b)=>+b.sample_count-+a.sample_count)
           : wSort.value==='name'?d.sort((a,b)=>a.scientific_name.localeCompare(b.scientific_name))
           : d.sort((a,b)=>+b.total_reads-+a.total_reads);
    }
    function toggleWAllowedRank(r){ const s=new Set(wAllowedRanks.value); if(s.has(r))s.delete(r); else s.add(r); wAllowedRanks.value=s; drawWilderChart(); drawWilderLolli(); }
    function wShowStdRanks(){ wAllowedRanks.value=new Set(wLoadedRanks.value.filter(r=>WILDER_STD_RANKS.has(r))); drawWilderChart(); drawWilderLolli(); }
    function wShowAllRanks(){ wAllowedRanks.value=new Set(wLoadedRanks.value); drawWilderChart(); drawWilderLolli(); }
    function drawWilderChart(){
      let data=wilderSortData().slice(0,wTopN.value);
      const hl=wilderTopHighlighted.value;
      if(hl.size) data=data.filter(d=>hl.has(d.scientific_name));
      const wTotal=d3.sum(data,d=>+d.total_reads)||1;
      drawBar('wilderTop', wilderChartSvg.value,wilderChartWrap.value,data,{
        labelKey:'scientific_name',valueKey:'total_reads',colorFn:d=>groupColor(d.organism_group),
        fmtVal:v=>`${d3.format(',.0f')(v)} (${(v/wTotal*100).toFixed(1)}%)`,italic:true,
        fmtTip:d=>`<strong><em>${d.scientific_name}</em></strong>${d.common_name?d.common_name+'<br>':''}Group: ${d.organism_group||'–'}<br>Reads: ${Number(d.total_reads).toLocaleString()}<br>Samples: ${d.sample_count}`,
        onClickBar:d=>toggleWilderTopHighlight(d.scientific_name),
      });
      const groups=[...new Set(data.map(d=>d.organism_group).filter(Boolean))];
      // Legend: derive from full taxa data so hidden groups remain visible for re-toggle
      const legendGroups=[...new Set(wilderTopTaxa.value.map(d=>d.organism_group).filter(Boolean))];
      buildLegend(wilderLegend.value,legendGroups.map(g=>({label:g,color:groupColor(g)})),{
        onToggle:g=>toggleWilderHiddenGroup(g), hiddenSet:wilderHiddenGroups.value });
    }
    function toggleWilderHiddenGroup(g){ const s=new Set(wilderHiddenGroups.value); if(s.has(g))s.delete(g); else s.add(g); wilderHiddenGroups.value=s; drawWilderChart(); drawWilderLolli(); }
    function drawWilderLolli(){
      let data=wilderSortData().sort((a,b)=>+b.sample_count-+a.sample_count).slice(0,wTopN.value);
      const hl=wilderTopHighlighted.value;
      if(hl.size) data=data.filter(d=>hl.has(d.scientific_name));
      drawLollipop(wilderLolliSvg.value,wilderLolliWrap.value,data,{
        labelKey:'scientific_name',valueKey:'sample_count',colorFn:d=>groupColor(d.organism_group),
        fmtTip:d=>`<strong><em>${d.scientific_name}</em></strong>Detected in ${d.sample_count} sample(s)<br>Group: ${d.organism_group||'–'}<br>Total reads: ${Number(d.total_reads).toLocaleString()}`,
        onClickBar:d=>toggleWilderTopHighlight(d.scientific_name),
      });
    }
    function drawGroupChart(){
      let data=[...wilderGroups.value];
      if(groupBarHiddenGroups.value.size>0) data=data.filter(x=>!groupBarHiddenGroups.value.has(x.organism_group));
      data=groupSort.value==='species'?data.sort((a,b)=>+b.species_count-+a.species_count)
               : groupSort.value==='name'?data.sort((a,b)=>a.organism_group.localeCompare(b.organism_group))
               : data.sort((a,b)=>+b.total_reads-+a.total_reads);
      const gTotal=d3.sum(data,d=>+d.total_reads)||1;
      drawBar('groupBar', groupBarSvg.value,groupBarWrap.value,data,{
        labelKey:'organism_group',valueKey:'total_reads',colorFn:d=>groupColor(d.organism_group),
        fmtVal:v=>`${d3.format(',.0f')(v)} (${(v/gTotal*100).toFixed(1)}%)`,
        fmtTip:d=>`<strong>${d.organism_group}</strong>${d.species_count} species<br>Total reads: ${Number(d.total_reads).toLocaleString()}`,
      });
      // Legend from ALL groups (so hidden ones can be re-enabled)
      const allGroups=[...wilderGroups.value].sort((a,b)=>+b.total_reads-+a.total_reads);
      buildLegend(groupBarLegend.value, allGroups.map(d=>({label:d.organism_group,color:groupColor(d.organism_group)})),{
        onToggle:g=>toggleGroupBarHiddenGroup(g), hiddenSet:groupBarHiddenGroups.value });
    }
    function toggleGroupBarHiddenGroup(g){ const s=new Set(groupBarHiddenGroups.value); if(s.has(g))s.delete(g); else s.add(g); groupBarHiddenGroups.value=s; drawGroupChart(); }

    // ── Wilder Sunburst ──────────────────────────────────────────────────────
    function buildWilderSunburstNodes(taxa){
      const nodes=[];
      nodes.push({taxid:0,parenttaxid:null,target:'All Species',full:'All Species',rank_code:'base',_direct_reads:0});
      const groupMap={};
      let gid=-1;
      for(const sp of taxa){
        const g=sp.organism_group||'Unknown';
        if(groupMap[g]===undefined){
          groupMap[g]=gid;
          nodes.push({taxid:gid,parenttaxid:0,target:g,full:g,rank_code:'G',_direct_reads:0,_group:g});
          gid--;
        }
      }
      taxa.forEach((sp,i)=>{
        const g=sp.organism_group||'Unknown';
        nodes.push({taxid:(i+1)*1000,parenttaxid:groupMap[g],target:sp.scientific_name,full:sp.scientific_name,rank_code:'S',_direct_reads:+(sp.total_reads||0),_group:g});
      });
      return nodes;
    }
    async function loadWilderSbSamples(){ try{ const r=await axios.get(`${API}/wilder/samples`); wilderSbSamples.value=r.data; }catch{} }
    function applyWilderSbFilters(panel){ const st=_wSbStates[panel.id]; if(st) applyK2SunburstFilters(st); }
    function drawWilderSbPanel(panel){
      const wColorFn=d=>d.rank_code==='G'?groupColor(d.target):groupColor(d._group||'Unknown');
      const st=_drawSunburstCore(wSbSvgEls[panel.id],wSbWrapEls[panel.id],wSbLegendEls[panel.id],panel.data,()=>panel.search,()=>false,0,wColorFn,wSbListEls[panel.id]);
      _wSbStates[panel.id]=st;
      if(wSbLegendEls[panel.id]&&st&&panel._taxa.length){
        const groups=[...new Set(panel._taxa.map(d=>d.organism_group||'Unknown'))];
        buildLegend(wSbLegendEls[panel.id],groups.map(g=>({label:g,color:groupColor(g)})));
      }
    }
    async function loadWilderSbPanel(panel){
      panel.loading=true;
      try{
        const p=new URLSearchParams({limit:500});
        if(panel.sample) p.set('mbon_data_id',panel.sample);
        if(panel.group) p.set('organism_group',panel.group);
        const r=await axios.get(`${API}/wilder/top-taxa?${p}`);
        const taxa=Array.isArray(r.data)?r.data:(r.data?.taxa||[]);
        panel._taxa=taxa;
        panel.data=buildWilderSunburstNodes(taxa);
      }catch(e){ panel.data=[];panel._taxa=[]; }
      finally{ panel.loading=false; }
      if(panel.data.length){ await nextTick(); drawWilderSbPanel(panel); }
    }
    function addWilderSbPanel(){ wilderSbPanels.value.push({id:_wSbNextId++,sample:'',group:'',data:[],loading:false,search:'',sampleSearch:'',_taxa:[]}); }
    function removeWilderSbPanel(id){ wilderSbPanels.value=wilderSbPanels.value.filter(p=>p.id!==id); delete wSbSvgEls[id]; delete wSbWrapEls[id]; delete wSbLegendEls[id]; delete wSbListEls[id]; delete _wSbStates[id]; }
    function resetWilderSbPanel(id){ const st=_wSbStates[id]; if(st) sbFocusOn(st.root,st); }

    // ── Wilderlab Compare ────────────────────────────────────────────────────
    const wilderCompareSamples=ref([]),wilderCompareSelected=ref([]);
    const wilderCompareGroup=ref(''),wilderCompareTop=ref(20);
    const wilderCompareData=ref(null),wilderCompareLoading=ref(false);
    const wilderCompareSvg=ref(null),wilderCompareWrap=ref(null),wilderCompareLegend=ref(null);
    const wilderComparePlotFilter=ref(''),wilderCompareTableSearch=ref(''),wilderCompareTableShowAll=ref(false);
    // Wilderlab Results highlight + per-sample comparison
    const wilderResHighlighted=ref(new Set());
    const wilderGroupsJobFilter=ref('');
    const wilderResCompareSvg=ref(null),wilderResCompareWrap=ref(null);

    async function loadWilderCompareSamples(){
      try{
        const r=await axios.get(`${API}/mbon/samples?limit=500`);
        wilderCompareSamples.value=r.data;
      }catch{}
    }
    async function loadWilderCompare(){
      if(wilderCompareSelected.value.length<2)return;
      wilderCompareLoading.value=true;
      try{
        const p=new URLSearchParams({samples:wilderCompareSelected.value.join(','),top:wilderCompareTop.value});
        if(wilderCompareGroup.value)p.set('group',wilderCompareGroup.value);
        const r=await axios.get(`${API}/wilder/compare?${p}`);
        wilderCompareData.value=r.data;
      }catch{ wilderCompareData.value=null; }
      finally{ wilderCompareLoading.value=false; }
      if(wilderCompareData.value?.matrix?.length){ await nextTick(); drawWilderCompareChart(); }
    }
    function drawWilderCompareChart(){
      if(!wilderCompareData.value?.matrix?.length)return;
      const {sample_ids,aliases,matrix}=wilderCompareData.value;
      const filt=wilderComparePlotFilter.value.toLowerCase().trim();
      const filtMatrix=filt?matrix.filter(row=>(row.name||'').toLowerCase().includes(filt)):matrix;
      drawGroupedBar(wilderCompareSvg.value,wilderCompareWrap.value,wilderCompareLegend.value,filtMatrix,sample_ids,{
        labelKey:'name',valuePrefix:'s',valueSuffix:'',pageKey:'wcmp',pageSize:15,
        colorFn:(id,gi)=>d3.schemeTableau10[gi%10],
        labelFn:id=>aliases?.[String(id)]||`Sample #${id}`,
        fmtVal:v=>d3.format(',.0f')(v),
        fmtTip:(row,id,val)=>`<strong>${row.name}</strong>${aliases?.[String(id)]||'Sample #'+id}<br>Reads: ${Number(val).toLocaleString()}<br>Group: ${row.group||'–'}`,
      });
    }

    // ── LCA highlight + cross-report compare ──────────────────────────
    function toggleK2LcaHighlight(name){
      const s=new Set(k2LcaHighlighted.value);
      if(s.has(name))s.delete(name); else s.add(name);
      k2LcaHighlighted.value=s;
      drawK2Chart();
    }
    function clearK2LcaHighlights(){ k2LcaHighlighted.value=new Set(); drawK2Chart(); }

    async function loadK2CrossCompare(name){
      if(!name)return;
      k2CrossName.value=name;
      k2CrossLoading.value=true;
      k2CrossData.value=null;
      try{
        const r=await axios.get(`${API}/kraken2/taxon-compare?name=${encodeURIComponent(name)}`);
        k2CrossData.value=r.data;
      }catch{ k2CrossData.value=[]; }
      finally{ k2CrossLoading.value=false; }
      if(k2CrossData.value?.length){ await nextTick(); drawK2CrossChart(); }
    }
    function drawK2CrossChart(){
      if(!k2CrossData.value?.length||!k2CrossSvg.value)return;
      // Simple horizontal bar: each report is one bar, value = abundance
      const data=k2CrossData.value.map(d=>({
        label:`#${d.report_id} ${d.database||''}`.trim(),
        abundance:+d.abundance||0,
        count:+d.count||0,
        report_id:d.report_id,
      })).sort((a,b)=>b.abundance-a.abundance);
      drawBar('k2cross', k2CrossSvg.value,k2CrossWrap.value,data,{
        labelKey:'label',valueKey:'abundance',colorFn:(_,i)=>d3.schemeTableau10[i%10],
        fmtVal:v=>v.toFixed(3)+'%',pct:true,
        fmtTip:d=>`<strong>${d.label}</strong>Abundance: ${d.abundance.toFixed(3)}%<br>Count: ${Number(d.count).toLocaleString()}`,
      });
    }
    function closeK2Cross(){ k2CrossName.value=''; k2CrossData.value=null; }

    // ── Wilderlab Top Taxa multiselect + per-sample comparison ──────────
    const wilderTopHighlighted=ref(new Set());
    const wilderTopCompareRows=ref([]),wilderTopCompareLoading=ref(false);
    const wilderTopCompareSvg=ref(null),wilderTopCompareWrap=ref(null);

    function toggleWilderTopHighlight(name){
      const s=new Set(wilderTopHighlighted.value);
      if(s.has(name))s.delete(name); else s.add(name);
      wilderTopHighlighted.value=s;
      drawWilderChart();
      drawWilderLolli();
      nextTick(loadWilderTopCompare);
    }
    function clearWilderTopHighlights(){
      wilderTopHighlighted.value=new Set();
      wilderTopCompareRows.value=[];
      drawWilderChart();
      drawWilderLolli();
    }
    async function loadWilderTopCompare(){
      if(!wilderTopHighlighted.value.size){ wilderTopCompareRows.value=[]; return; }
      wilderTopCompareLoading.value=true;
      try{
        const p=new URLSearchParams({names:[...wilderTopHighlighted.value].join(','),limit:2000});
        const r=await axios.get(`${API}/wilder/results?${p}`);
        wilderTopCompareRows.value=r.data;
        // watcher on wilderTopCompareData (flush:'post') handles the redraw
      }catch{}finally{ wilderTopCompareLoading.value=false; }
    }
    const wilderTopCompareData=computed(()=>{
      if(!wilderTopHighlighted.value.size||!wilderTopCompareRows.value.length)return null;
      const names=[...wilderTopHighlighted.value];
      const sampleKeys=[...new Set(
        wilderTopCompareRows.value.map(r=>JSON.stringify({j:r.wilder_job_id,a:r.mbon_samp_alias||r.wilder_uid||r.wilder_job_id}))
      )].map(k=>JSON.parse(k));
      if(!sampleKeys.length)return null;
      const matrix=names.map(name=>{
        const row={name};
        sampleKeys.forEach(sk=>{
          const key=sk.j+'|'+sk.a;
          const match=wilderTopCompareRows.value.find(r=>
            r.scientific_name===name&&r.wilder_job_id===sk.j&&
            (r.mbon_samp_alias||r.wilder_uid||r.wilder_job_id)===sk.a
          );
          row[key]=match?+match.read_count:0;
        });
        return row;
      });
      return{sampleKeys,matrix};
    });
    function drawWilderTopCompare(){
      const d=wilderTopCompareData.value;
      if(!d||!wilderTopCompareSvg.value)return;
      const ids=d.sampleKeys.map(sk=>sk.j+'|'+sk.a);
      drawGroupedBar(wilderTopCompareSvg.value,wilderTopCompareWrap.value,null,d.matrix,ids,{
        labelKey:'name',valuePrefix:'',valueSuffix:'',
        colorFn:(_,gi)=>d3.schemeTableau10[gi%10],
        labelFn:id=>{const sk=d.sampleKeys.find(s=>s.j+'|'+s.a===id);return sk?`${sk.j} / ${sk.a}`:id;},
        fmtVal:v=>d3.format(',.0f')(v),
        fmtTip:(row,id,val)=>{const sk=d.sampleKeys.find(s=>s.j+'|'+s.a===id);return`<strong>${row.name}</strong>${sk?sk.j+' / '+sk.a:id}<br>Reads: ${Number(val).toLocaleString()}`;},
      });
    }

    // ── Wilderlab Results highlight + per-sample comparison ───────────
    function toggleWilderResHighlight(name){
      const s=new Set(wilderResHighlighted.value);
      if(s.has(name))s.delete(name); else s.add(name);
      wilderResHighlighted.value=s;
      // watcher on wilderResCompareData (flush:'post') handles the redraw
    }
    function clearWilderResHighlights(){ wilderResHighlighted.value=new Set(); }

    const wilderResCompareData=computed(()=>{
      if(!wilderResHighlighted.value.size)return null;
      const names=[...wilderResHighlighted.value];
      // Use job+alias as sample key so same alias in different jobs is distinct
      const sampleKeys=[...new Set(
        wilderResults.value.map(r=>JSON.stringify({j:r.wilder_job_id,a:r.mbon_samp_alias||r.wilder_uid||r.wilder_job_id}))
      )].map(k=>JSON.parse(k));
      if(!sampleKeys.length)return null;
      const matrix=names.map(name=>{
        const row={name};
        sampleKeys.forEach(sk=>{
          const key=sk.j+'|'+sk.a;
          const match=wilderResults.value.find(r=>
            r.scientific_name===name &&
            r.wilder_job_id===sk.j &&
            (r.mbon_samp_alias||r.wilder_uid||r.wilder_job_id)===sk.a
          );
          row[key]=match?+match.read_count:0;
        });
        return row;
      });
      return{sampleKeys,matrix};
    });

    function drawWilderResCompare(){
      const d=wilderResCompareData.value;
      if(!d||!wilderResCompareSvg.value)return;
      const ids=d.sampleKeys.map(sk=>sk.j+'|'+sk.a);
      drawGroupedBar(wilderResCompareSvg.value,wilderResCompareWrap.value,null,d.matrix,ids,{
        labelKey:'name',valuePrefix:'',valueSuffix:'',
        colorFn:(_,gi)=>d3.schemeTableau10[gi%10],
        labelFn:id=>{const sk=d.sampleKeys.find(s=>s.j+'|'+s.a===id); return sk?`${sk.j} / ${sk.a}`:id;},
        fmtVal:v=>d3.format(',.0f')(v),
        fmtTip:(row,id,val)=>{const sk=d.sampleKeys.find(s=>s.j+'|'+s.a===id);return `<strong>${row.name}</strong>${sk?sk.j+' / '+sk.a:id}<br>Reads: ${Number(val).toLocaleString()}`;},
      });
    }

    // ── Name-filter tag helpers ───────────────────────────────────────
    function onK2NameKey(e){
      if(e.key!=='Enter'&&e.key!==',')return; e.preventDefault();
      const v=k2NameFilter.value.trim().replace(/,$/,'');
      if(v&&!k2NameTags.value.includes(v)) k2NameTags.value.push(v);
      k2NameFilter.value='';
      if(k2Lcas.value.length) drawK2Chart();
    }
    function removeK2Tag(tag){ k2NameTags.value=k2NameTags.value.filter(t=>t!==tag); if(k2Lcas.value.length) drawK2Chart(); }
    function clearK2Tags(){ k2NameTags.value=[]; if(k2Lcas.value.length) drawK2Chart(); }

    function onK2TopNameKey(e){
      if(e.key!=='Enter'&&e.key!==',')return; e.preventDefault();
      const v=k2TopNameFilter.value.trim().replace(/,$/,'');
      if(v&&!k2TopNameTags.value.includes(v)) k2TopNameTags.value.push(v);
      k2TopNameFilter.value='';
      if(k2TopTaxa.value.length) drawK2TopChart();
    }
    function removeK2TopTag(tag){ k2TopNameTags.value=k2TopNameTags.value.filter(t=>t!==tag); if(k2TopTaxa.value.length) drawK2TopChart(); }
    function clearK2TopTags(){ k2TopNameTags.value=[]; if(k2TopTaxa.value.length) drawK2TopChart(); }

    function onWNameKey(e){
      if(e.key!=='Enter'&&e.key!==',')return; e.preventDefault();
      const v=wNameFilter.value.trim().replace(/,$/,'');
      if(v&&!wNameTags.value.includes(v)) wNameTags.value.push(v);
      wNameFilter.value='';
      if(wilderTopTaxa.value.length){ drawWilderChart(); drawWilderLolli(); }
    }
    function removeWTag(tag){ wNameTags.value=wNameTags.value.filter(t=>t!==tag); if(wilderTopTaxa.value.length){ drawWilderChart(); drawWilderLolli(); } }
    function clearWTags(){ wNameTags.value=[]; if(wilderTopTaxa.value.length){ drawWilderChart(); drawWilderLolli(); } }

    // ── Kraken2 Sunburst (uses /api/kraken2/sunburst/<id>, d3.stratify) ───────
    async function _processSunburstNodes(rawNodes){
      const nodes=rawNodes;
      const childCladeSums={};
      nodes.forEach(n=>{ if(n.parenttaxid!=null) childCladeSums[n.parenttaxid]=(childCladeSums[n.parenttaxid]||0)+(n.num_fragments_clade||0); });
      nodes.forEach(n=>{ n._direct_reads=Math.max(0,(n.num_fragments_clade||0)-(childCladeSums[n.taxid]||0)); });
      const rootNode=nodes.find(n=>n.parenttaxid==null);
      // Track unclassified reads on root for display, but don't inject a node — show only classified tree
      if(rootNode){ rootNode._unclassified_reads=rootNode._direct_reads; rootNode._direct_reads=0; }
      return nodes;
    }
    async function loadSbPanel(panel){
      if(!panel.report)return;
      panel.loading=true;
      try{
        const r=await axios.get(`${API}/kraken2/sunburst/${panel.report}`);
        const nodes=await _processSunburstNodes(r.data);
        panel.data=nodes;
        const rn=nodes.find(n=>n.parenttaxid==null);
        panel.unclassReads=rn?(rn._unclassified_reads||0):0;
      }catch(e){ panel.data=[]; panel.unclassReads=0; }
      finally{ panel.loading=false; }
      if(panel.data.length){ await nextTick(); drawSbPanel(panel); }
    }
    // Backward compat: load the first panel (used by tab watcher fallback)
    async function loadK2SunburstData(){ await loadSbPanel(k2SbPanels.value[0]); }

    // Persistent sunburst state — survives filter-only updates so zoom is preserved
    let _sbState=null;
    function sbBaseOpacOf(d){ return d.depth<=1?0.88:0.55; }
    function sbIsDesc(n,a){ let x=n; while(x){ if(x===a)return true; x=x.parent; } return false; }

    // Build/update scrollable children list for the currently focused node
    function updateSbList(st, p){
      const el=st.listEl;
      if(!el)return;
      el.innerHTML='';
      const {root,colorFn}=st;
      const children=(p&&p.children)||[];
      const hdr=document.createElement('div');
      hdr.style.cssText='font-size:.72rem;font-weight:600;color:#555;padding:.25rem .5rem;background:var(--surface2,#eef0f3);border-bottom:1px solid var(--border,#e5e7eb);position:sticky;top:0;z-index:1';
      const nm=!p||p===root?(p?p.data.full||p.data.target||'Root':'Root'):(p.data.full||p.data.target||'');
      hdr.textContent=children.length?`${nm} · ${children.length} item${children.length!==1?'s':''}`:nm+' (leaf)';
      el.appendChild(hdr);
      if(!children.length){
        const row=document.createElement('div');
        row.style.cssText='padding:.35rem .5rem;font-size:.78rem;color:#777;font-style:italic';
        row.textContent=`${d3.format(',.0f')(p&&p.value||0)} reads`;
        el.appendChild(row);
        return;
      }
      const total=p?p.value||1:1;
      const sorted=[...children].sort((a,b)=>b.value-a.value);
      sorted.forEach(c=>{
        const name=c.data.full||c.data.target||'';
        const pct=(c.value/total*100).toFixed(1);
        const color=colorFn?colorFn(c.data):rankColor(c.data.rank_code);
        const row=document.createElement('div');
        row.style.cssText='display:flex;align-items:center;gap:.35rem;padding:.2rem .45rem;cursor:pointer;transition:background .1s';
        row.onmouseenter=()=>{row.style.background='var(--surface,#f5f7fa)'};
        row.onmouseleave=()=>{row.style.background=''};
        row.onclick=()=>sbFocusOn(c,st);
        const dot=document.createElement('span');
        dot.style.cssText=`display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};flex-shrink:0`;
        const nameEl=document.createElement('span');
        nameEl.style.cssText='flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:.78rem';
        if(c.data.rank_code==='S')nameEl.style.fontStyle='italic';
        nameEl.textContent=name;
        const pctEl=document.createElement('span');
        pctEl.style.cssText='color:#777;white-space:nowrap;font-size:.75rem;min-width:110px;text-align:right;font-variant-numeric:tabular-nums';
        pctEl.textContent=`${d3.format(',.0f')(c.value)} (${pct}%)`;
        row.append(dot,nameEl,pctEl);
        el.appendChild(row);
      });
    }

    // Compute opacity for a node given current filter + zoom context
    function sbNodeOpac(d, focused, root, mainOnly, searchTerm){
      const rankOk=!mainOnly||MAIN_RANKS.has(d.data.rank_code);
      const nm=(d.data.full||d.data.target||'').toLowerCase();
      const searchOk=!searchTerm||nm.includes(searchTerm);
      if(!rankOk||!searchOk)return 0.04;
      if(focused&&focused!==root)return(d===focused||sbIsDesc(d,focused))?sbBaseOpacOf(d):0.06;
      return sbBaseOpacOf(d);
    }

    // Zoom to node p — state-aware (defaults to _sbState for backward compat)
    // _syncOthers: when true, sync all other loaded panels to the same taxon
    function sbFocusOn(p, st, _syncOthers=true){
      if(!st)st=_sbState; if(!st)return;
      const{paths,xSc,ySc,maxR,arc,root,clName,clSub,visNodes}=st;
      st.focusedNode=p;
      const nm=p===root?'Taxonomy':(p.data.full||p.data.target||'');
      clName.text(nm.length>18?nm.slice(0,17)+'…':nm);
      clSub.text(p===root?`${visNodes.length} taxa`:`${d3.format(',.0f')(p.value||0)} reads`);
      const mainOnly=st.getMainRanks(); const searchTerm=st.getSearch().toLowerCase().trim();
      paths.transition().duration(750).ease(d3.easeCubicInOut)
        .tween('scale',()=>{
          const xd=d3.interpolate(xSc.domain(),[p.x0,p.x1]);
          const yd=d3.interpolate(ySc.domain(),[p.y0,1]);
          const yr=d3.interpolate(ySc.range(),[p.depth?maxR*0.12:maxR*0.04,maxR]);
          return t=>{ xSc.domain(xd(t)); ySc.domain(yd(t)).range(yr(t)); };
        })
        .attrTween('d',function(d){ return ()=>arc(d); })
        .attr('fill-opacity',d=>sbNodeOpac(d,p,root,mainOnly,searchTerm));
      updateSbList(st, p);
      // Sync all other panels to the same taxon
      if(_syncOthers){
        const taxonName=p===root?null:(p.data.full||p.data.target||'');
        Object.values(_sbStates).forEach(other=>{
          if(!other||other===st)return;
          if(!taxonName){ sbFocusOn(other.root,other,false); return; }
          const all=other.root.descendants();
          const match=all.find(d=>(d.data.full||d.data.target||'')===taxonName)
                    ||all.find(d=>(d.data.full||d.data.target||'').toLowerCase().startsWith(taxonName.toLowerCase()));
          sbFocusOn(match||other.root,other,false);
        });
      }
    }

    // Filter-only update — state-aware
    function applyK2SunburstFilters(st){
      if(!st)st=_sbState; if(!st)return;
      const{paths,root}=st;
      const mainOnly=st.getMainRanks(); const searchTerm=st.getSearch().toLowerCase().trim();
      const focused=st.focusedNode;
      paths.transition().duration(200)
        .attr('fill-opacity',d=>sbNodeOpac(d,focused,root,mainOnly,searchTerm));
      if(searchTerm.length>=3){
        const all=root.descendants();
        const best=all.find(d=>(d.data.full||d.data.target||'').toLowerCase()===searchTerm)
                 ||all.find(d=>(d.data.full||d.data.target||'').toLowerCase().startsWith(searchTerm))
                 ||all.find(d=>(d.data.full||d.data.target||'').toLowerCase().includes(searchTerm));
        if(best) sbFocusOn(best.parent&&best.parent.depth>0?best.parent:best, st);
      }
    }

    // ── Shared sunburst builder (half-crescent dome shape) ────────────────────
    function _drawSunburstCore(svgEl, wrapEl, legendEl, data, getSearch, getMainRanks, unclassReads=0, colorFn=null, listEl=null){
      if(!svgEl||!wrapEl||!data||!data.length)return null;
      const W=Math.max(wrapEl.clientWidth||0, 320);
      const SIZE=Math.min(W, 520);
      const maxR=SIZE/2-8;
      let root;
      try{
        root=d3.stratify().id(d=>String(d.taxid)).parentId(d=>d.parenttaxid!=null?String(d.parenttaxid):null)(data);
      }catch(e){ console.warn('Sunburst stratify error:',e.message); return null; }
      root.sum(d=>Math.max(0,+(d._direct_reads||0)));
      d3.partition().size([1,1])(root);
      // Half-crescent (dome): [-π/2, π/2] range — flat bottom, arcs fan upward
      const xSc=d3.scaleLinear().domain([0,1]).range([-Math.PI/2,Math.PI/2]);
      const ySc=d3.scaleSqrt().domain([0,1]).range([maxR*0.04,maxR]);
      const arc=d3.arc()
        .startAngle(d=>Math.max(-Math.PI/2,Math.min(Math.PI/2,xSc(d.x0))))
        .endAngle(d=>Math.max(-Math.PI/2,Math.min(Math.PI/2,xSc(d.x1))))
        .innerRadius(d=>Math.max(0,ySc(d.y0))).outerRadius(d=>Math.max(0,ySc(d.y1)))
        .padAngle(0.003).padRadius(maxR/2).cornerRadius(3);
      // SVG height = radius + padding for center label below the baseline
      const SVGh=maxR+40;
      const svg=d3.select(svgEl).attr('width',W).attr('height',SVGh);
      svg.selectAll('*').remove();
      const g=svg.append('g').attr('transform',`translate(${W/2},${maxR+8})`);
      const visNodes=root.descendants().filter(d=>d.depth>0&&d.x1>d.x0);
      const paths=g.selectAll('path').data(visNodes).join('path')
        .attr('fill',d=>colorFn?colorFn(d.data):rankColor(d.data.rank_code))
        .attr('stroke','#fff').attr('stroke-width',0.5).style('cursor','pointer').attr('d',arc);
      // Center circle — click goes UP one level (not to root)
      const innerR=Math.max(14,ySc(0.04));
      const cg=g.append('g').style('cursor','pointer');
      cg.append('circle').attr('r',innerR).attr('fill','white').attr('fill-opacity',0.95);
      const cl=cg.append('text').attr('text-anchor','middle').attr('fill','#333').style('pointer-events','none').style('user-select','none');
      const clName=cl.append('tspan').attr('x',0).attr('dy','-.3em').attr('font-size',11).attr('font-weight',700).text('Taxonomy');
      const clSub =cl.append('tspan').attr('x',0).attr('dy','1.4em').attr('font-size',9).attr('fill','#888').text(`${visNodes.length} taxa`);
      const st={root,paths,xSc,ySc,maxR,arc,visNodes,clName,clSub,focusedNode:null,getSearch,getMainRanks,colorFn,listEl};
      const mainOnly=getMainRanks(); const searchTerm=getSearch().toLowerCase().trim();
      paths.attr('fill-opacity',d=>sbNodeOpac(d,null,root,mainOnly,searchTerm));
      paths.on('mouseover',(e,d)=>{
          const nm=d.data.full||d.data.target||`taxid:${d.data.taxid}`;
          const pct=(d.value/(root.value||1)*100).toFixed(2);
          showTip(e,`<strong>${nm}</strong>Rank: ${rankLabel(d.data.rank_code)}<br>Reads: ${d3.format(',.0f')(d.value||0)}<br>${pct}%<br><em style="opacity:.7;font-size:.7rem">Click to zoom · center = up one level</em>`);
        })
        .on('mousemove',moveTip).on('mouseleave',hideTip)
        .on('click',(e,d)=>{ e.stopPropagation(); sbFocusOn(d,st); });
      // Center click → go UP one level (to parent of current focus, or root if already at root)
      cg.on('click',()=>{
        const focused=st.focusedNode;
        const target=focused&&focused!==root&&focused.parent?focused.parent:root;
        sbFocusOn(target,st);
      });
      // Entrance animation — arcs fan in from center
      paths.each(function(d){ this._x0=d.x0; this._x1=d.x0; this._y0=d.y0; this._y1=d.y1; });
      paths.transition().duration(700).attrTween('d',function(d){
        const i=d3.interpolate({x0:this._x0,x1:this._x1,y0:this._y0,y1:this._y1},d);
        return t=>arc(i(t));
      });
      const rankCodes=[...new Set(visNodes.map(d=>d.data.rank_code).filter(r=>MAIN_RANKS.has(r)))];
      if(legendEl){
        buildLegend(legendEl, rankCodes.map(r=>({label:rankLabel(r),color:rankColor(r)})));
        if(unclassReads>0){
          const div=document.createElement('div');
          div.style.cssText='font-size:.73rem;color:#888;margin-top:.35rem;padding:.15rem .35rem;background:var(--surface,#f8f9fa);border-radius:4px;border:1px solid var(--border,#e5e7eb)';
          div.textContent=`Unclassified: ${d3.format(',.0f')(unclassReads)} reads`;
          legendEl.appendChild(div);
        }
      }
      updateSbList(st, root);
      return st;
    }

    // Draw any panel by ID — uses sbSvgEls/sbWrapEls/sbLegendEls populated by Vue function refs
    function drawSbPanel(panel){
      const st=_drawSunburstCore(
        sbSvgEls[panel.id], sbWrapEls[panel.id], sbLegendEls[panel.id],
        panel.data, ()=>panel.search, ()=>panel.mainRanks, panel.unclassReads, null, sbListEls[panel.id]
      );
      _sbStates[panel.id]=st;
      _sbState=st; // keep _sbState pointing at last drawn (compat)
    }
    // Backward compat wrapper
    function drawK2Sunburst(){ if(k2SbPanels.value.length) drawSbPanel(k2SbPanels.value[0]); }

    // ── Kraken2 Compare ──────────────────────────────────────────────────────
    async function loadK2Compare(){
      if(k2CompareSelected.value.length<2)return;
      k2CompareLoading.value=true;
      try{
        const p=new URLSearchParams({reports:k2CompareSelected.value.join(','),rank:k2CompareRank.value,top:k2CompareTop.value});
        const r=await axios.get(`${API}/kraken2/compare?${p}`);
        k2CompareData.value=r.data;
      }catch{ k2CompareData.value=null; }
      finally{ k2CompareLoading.value=false; }
      if(k2CompareData.value?.matrix?.length){ await nextTick(); drawK2CompareChart(); }
    }
    function drawK2CompareChart(){
      if(!k2CompareData.value?.matrix?.length)return;
      const {report_ids,meta,matrix}=k2CompareData.value;
      const filt=k2ComparePlotFilter.value.toLowerCase().trim();
      const filtMatrix=filt?matrix.filter(row=>(row.name||'').toLowerCase().includes(filt)):matrix;
      drawGroupedBar(k2CompareSvg.value,k2CompareWrap.value,k2CompareLegend.value,filtMatrix,report_ids,{
        labelKey:'name',valuePrefix:'r',valueSuffix:'_abundance',pageKey:'k2cmp',pageSize:15,
        colorFn:(id,gi)=>d3.schemeTableau10[gi%10],
        labelFn:id=>{ const m=meta?.[String(id)]; if(!m)return`Report #${id}`; return m.sample_alias?`${m.sample_alias} (#${id})`:`#${id} ${m.database||''}`.trim(); },
        fmtVal:v=>v.toFixed(2)+'%',pct:true,
        fmtTip:(row,id,val)=>{ const m=meta?.[String(id)]; return `<strong>${row.name}</strong>Report #${id}${m?.sample_alias?`<br>${m.sample_alias}`:''}${m?.station?`<br>Station: ${m.station}`:''}<br>Abundance: ${val.toFixed(3)}%<br>Count: ${(row['r'+id+'_count']||0).toLocaleString()}`; },
      });
    }

    // ── Map view ──────────────────────────────────────────────────────
    // ── Map state ─────────────────────────────────────────────────────
    const mapAvailableTypes = ref([]);
    const mapK2RankOptions = ref([]);
    const mapActiveTypes = ref(['wilder']);
    const mapStations = ref([]);
    const mapLoading = ref(false);
    const mapActiveStation = ref(null);
    const mapSelectedStations = ref(new Set());
    const mapWilderJob = ref('');
    const mapWilderGroup = ref('');
    const mapK2Rank = ref('S');
    const mapK2Group = ref('');              // toolbar: Kraken2 organism-group filter (server-side)
    const mapK2Groups = ref([]);             // list of {organism_group, taxa_count}
    const exK2Groups = ref([]);              // query-panel: selected Kraken2 groups (client-side marker filter)
    // Declared here (not in the Explore section) because filteredMapStations reads
    // it and is evaluated during setup by its watcher — must exist before then.
    const exSpecies = ref([]);               // query-panel: pinned species names
    const mapTopN = ref(20);
    const mapPopupChartSvg = ref(null);
    const mapPopupChartWrap = ref(null);
    const mapSamplesOpen = ref(false);
    const mapPopupK2Rank = ref('S');          // rank filter inside popup chart (Kraken2)
    const mapPopupWilderView = ref('species'); // 'species' | 'groups'  view toggle for Wilder popup
    const mapPopupTab = ref('distribution');
    const mapStnHighlighted = ref(new Set()); // multi-select highlight set for popup table

    // ── Cross-sample merging (ported from build_mbon_html.py template) ──
    // Let the user pick a subset of a station's samples and MERGE them into a
    // single combined distribution (server-side aggregation), with the number
    // of merged samples shown. mapMergeResult holds the endpoint payload.
    const mapMergeSelected = ref(new Set());   // mbon_data_id set chosen to merge
    const mapMergeResult   = ref(null);        // { type, sample_count, total_reads, taxa, groups }
    const mapMergeLoading  = ref(false);
    const mapMergeError    = ref('');
    const mapMergeView     = ref('combined');  // 'combined' | 'persample' (cross-sample)

    // Popup drag/resize state
    const mapPopupPos = ref(null); // null = use default CSS (top:10px right:10px)
    const mapPopupSize = ref({ w: 420, h: 520 });
    let _dragState = null, _resizeState = null;

    const mapPopupStyle = computed(() => {
      const sz = mapPopupSize.value;
      const pos = mapPopupPos.value;
      const base = { width: sz.w + 'px', height: sz.h + 'px' };
      if (pos) return { ...base, top: pos.y + 'px', left: pos.x + 'px', right: 'auto' };
      return { ...base, top: '10px', right: '10px', left: 'auto' };
    });

    function mapPopupDragStart(e) {
      if (e.button !== 0) return;
      if (e.target.closest('button,a,.map-popup-resize-handle,select,input')) return;
      const panel = e.currentTarget.closest('.map-popup-panel');
      if (!panel) return;
      const rect = panel.getBoundingClientRect();
      const cont = panel.parentElement.getBoundingClientRect();
      _dragState = { sx: e.clientX, sy: e.clientY, px: rect.left - cont.left, py: rect.top - cont.top };
      mapPopupPos.value = { x: _dragState.px, y: _dragState.py };
      window.addEventListener('mousemove', _mapDragMove);
      window.addEventListener('mouseup', _mapDragEnd);
      e.preventDefault();
    }
    function mapPopupResizeStart(e) {
      if (e.button !== 0) return;
      const sz = mapPopupSize.value;
      _resizeState = { sx: e.clientX, sy: e.clientY, sw: sz.w, sh: sz.h };
      window.addEventListener('mousemove', _mapDragMove);
      window.addEventListener('mouseup', _mapDragEnd);
      e.preventDefault();
    }
    function _mapDragMove(e) {
      if (_dragState) {
        mapPopupPos.value = {
          x: Math.max(0, _dragState.px + e.clientX - _dragState.sx),
          y: Math.max(0, _dragState.py + e.clientY - _dragState.sy),
        };
      }
      if (_resizeState) {
        mapPopupSize.value = {
          w: Math.max(280, _resizeState.sw + e.clientX - _resizeState.sx),
          h: Math.max(200, _resizeState.sh + e.clientY - _resizeState.sy),
        };
      }
    }
    function _mapDragEnd() {
      _dragState = null; _resizeState = null;
      window.removeEventListener('mousemove', _mapDragMove);
      window.removeEventListener('mouseup', _mapDragEnd);
    }

    // Map name filter state
    const mapWilderNameFilter = ref('');
    const mapWilderNameTags = ref([]);
    const mapWilderSuggestions = ref([]);
    const mapK2NameFilter = ref('');
    const mapK2NameTags = ref([]);
    const mapK2Suggestions = ref([]);
    const mapStnSearch = ref('');

    // ── Map station compare ────────────────────────────────────────────
    const mapCompareOpen   = ref(false);
    const mapCompareView   = ref('perstation'); // 'perstation' | 'aggregate'
    const mapCompareTopN   = ref(20);
    const mapCompareK2Rank = ref('S');
    const mapCompareGroup  = ref('');
    const mapCompareSvg    = ref(null);
    const mapCompareWrap   = ref(null);
    const mapCompareLegend = ref(null);

    // filteredSortedMapSpecies — search only; group/species view handled in drawMapPopupChart
    const filteredSortedMapSpecies = computed(() => {
      if (!mapActiveStation.value) return [];
      const rows = applySort(tableSorts.value['mapstn'] || { key: 'total_reads', dir: 'desc' }, mapActiveStation.value.top_species || []);
      const s = mapStnSearch.value.toLowerCase();
      if (!s) return rows;
      return rows.filter(x =>
        (x.scientific_name || '').toLowerCase().includes(s) ||
        (x.common_name || '').toLowerCase().includes(s) ||
        (x.organism_group || '').toLowerCase().includes(s)
      );
    });

    // Aggregated groups for Wilder "Groups" view (used by table and chart)
    const mapPopupGroupedData = computed(() => {
      if (!mapActiveStation.value) return [];
      const rows = filteredSortedMapSpecies.value;
      const grpMap = {};
      for (const sp of rows) {
        const g = sp.organism_group || 'Unknown';
        if (!grpMap[g]) grpMap[g] = { organism_group: g, total_reads: 0, species_count: 0 };
        grpMap[g].total_reads += +(sp.total_reads || 0);
        grpMap[g].species_count++;
      }
      return Object.values(grpMap).sort((a, b) => b.total_reads - a.total_reads);
    });
    const filteredSortedMapTaxa = computed(() => {
      if (!mapActiveStation.value) return [];
      let rows = applySort(tableSorts.value['mapstn'] || { key: 'total_reads', dir: 'desc' }, mapActiveStation.value.top_taxa || []);
      const rk = mapPopupK2Rank.value;
      if (rk) rows = rows.filter(x => x.rank === rk);
      const s = mapStnSearch.value.toLowerCase();
      if (!s) return rows;
      return rows.filter(x =>
        (x.name || '').toLowerCase().includes(s) ||
        (x.rank || '').toLowerCase().includes(s)
      );
    });
    const mapPopupK2RankOptions = computed(() => sortRanks(mapActiveStation.value?.rank_options || (mapActiveStation.value?.top_taxa || []).map(x => x.rank)));
    const mapCompareK2RankOptions = computed(() => sortRanks(selectedStationObjects.value.flatMap(stn => (stn.top_taxa || []).map(x => x.rank))));
    const mapPopupTitle = computed(() => {
      if (!mapActiveStation.value) return '';
      if (mapPopupTab.value === 'compare') return 'Station Comparison';
      if (mapPopupTab.value === 'depth') return 'Sample Depth Comparison';
      return mapActiveStation.value._type === 'wilder' ? 'Top Species by Reads' : 'Top Taxa by Reads';
    });

    // filteredMapStations — apply name-tag filters
    const filteredMapStations = computed(() => {
      // Query-panel taxa filters that also apply to Kraken2 markers (client-side).
      const qGroups  = exK2Groups.value;                       // Kraken2 groups
      const qSpecies = (exSpecies.value || []).map(s => s.toLowerCase());
      return mapStations.value.filter(stn => {
        if (stn._type === 'wilder') {
          if (!mapWilderNameTags.value.length) return true;
          const tags = mapWilderNameTags.value.map(t => t.toLowerCase());
          return (stn.top_species || []).some(sp =>
            tags.some(t => (sp.scientific_name || '').toLowerCase().includes(t) || (sp.common_name || '').toLowerCase().includes(t))
          );
        } else {
          const taxa = stn.top_taxa || [];
          // Toolbar name tags
          if (mapK2NameTags.value.length) {
            const tags = mapK2NameTags.value.map(t => t.toLowerCase());
            if (!taxa.some(tx => tags.some(t => (tx.name || '').toLowerCase().includes(t)))) return false;
          }
          // Query-panel Kraken2 group selection
          if (qGroups.length && !taxa.some(tx => qGroups.includes(tx.organism_group))) return false;
          // Query-panel species names (match Kraken2 taxon names)
          if (qSpecies.length && !taxa.some(tx => qSpecies.some(q => (tx.name || '').toLowerCase().includes(q)))) return false;
          return true;
        }
      });
    });

    // Selected station objects — maps selected keys back to station data
    const selectedStationObjects = computed(() => {
      if (!mapSelectedStations.value.size) return [];
      const keySet = mapSelectedStations.value;
      return mapStations.value.filter(s => keySet.has(mapStationKey(s)));
    });

    // Autocomplete helpers
    function refreshMapWilderSuggestions() {
      const q = (mapWilderNameFilter.value || '').toLowerCase();
      if (!q || q.length < 2) { mapWilderSuggestions.value = []; return; }
      const seen = new Set(), out = [];
      for (const s of mapStations.value) {
        for (const sp of (s.top_species || [])) {
          for (const nm of [sp.scientific_name, sp.common_name]) {
            if (nm && nm.toLowerCase().includes(q) && !seen.has(nm)) { seen.add(nm); out.push(nm); if (out.length >= 20) break; }
          }
          if (out.length >= 20) break;
        }
        if (out.length >= 20) break;
      }
      mapWilderSuggestions.value = out;
    }
    function refreshMapK2Suggestions() {
      const q = (mapK2NameFilter.value || '').toLowerCase();
      if (!q || q.length < 2) { mapK2Suggestions.value = []; return; }
      const seen = new Set(), out = [];
      for (const s of mapStations.value) {
        for (const tx of (s.top_taxa || [])) {
          if (tx.name && tx.name.toLowerCase().includes(q) && !seen.has(tx.name)) { seen.add(tx.name); out.push(tx.name); if (out.length >= 20) break; }
        }
        if (out.length >= 20) break;
      }
      mapK2Suggestions.value = out;
    }
    function onMapWilderNameKey(e) {
      if (e.key !== 'Enter' && e.key !== ',') return; e.preventDefault();
      const v = mapWilderNameFilter.value.trim().replace(/,$/, '');
      if (v && !mapWilderNameTags.value.includes(v)) mapWilderNameTags.value.push(v);
      mapWilderNameFilter.value = ''; mapWilderSuggestions.value = [];
    }
    function removeMapWilderTag(tag) { mapWilderNameTags.value = mapWilderNameTags.value.filter(t => t !== tag); }
    function clearMapWilderTags() { mapWilderNameTags.value = []; }
    function onMapK2NameKey(e) {
      if (e.key !== 'Enter' && e.key !== ',') return; e.preventDefault();
      const v = mapK2NameFilter.value.trim().replace(/,$/, '');
      if (v && !mapK2NameTags.value.includes(v)) mapK2NameTags.value.push(v);
      mapK2NameFilter.value = ''; mapK2Suggestions.value = [];
    }
    function removeMapK2Tag(tag) { mapK2NameTags.value = mapK2NameTags.value.filter(t => t !== tag); }
    function clearMapK2Tags() { mapK2NameTags.value = []; }

    let _leafletMap = null;
    let _leafletMarkers = [];
    let _leafletClusterGroup = null;    // leaflet.markercluster group (numbered bubbles when zoomed out)
    let _activeMarker = null;
    const _selectedMarkers = new Map(); // station key -> marker ref
    const CLUSTER_RING_COLORS = ['#2563eb', '#e07b39', '#16a34a', '#9333ea', '#dc2626', '#0891b2', '#ca8a04', '#475569'];

    function mapHashColor(value) {
      const text = String(value || 'Unknown');
      let hash = 0;
      for (let index = 0; index < text.length; index++) hash = ((hash << 5) - hash) + text.charCodeAt(index);
      return CLUSTER_RING_COLORS[Math.abs(hash) % CLUSTER_RING_COLORS.length];
    }

    function mapClusterEntries(markers) {
      const entries = [];
      markers.forEach(marker => {
        const stn = marker._stn || {};
        const samples = stn.samples && stn.samples.length ? stn.samples : [{ alias: stn.station, mbon_data_id: mapStationKey(stn) }];
        samples.forEach(sample => {
          entries.push({
            project: sample.project_name || stn.project_name || stn._type || 'Unknown project',
            run: sample.run_directory || sample.run_id || sample.job_id || sample.report_id || 'Unknown run',
            sample: sample.alias || sample.mbon_data_id || stn.station || 'Sample',
            type: stn._type || '',
          });
        });
      });
      return entries;
    }

    function mapConicSlices(items, keyFn, colorFn) {
      const counts = new Map();
      items.forEach(item => {
        const key = keyFn(item);
        counts.set(key, (counts.get(key) || 0) + 1);
      });
      const total = Math.max(items.length, 1);
      let cursor = 0;
      const slices = [...counts.entries()].sort((a, b) => String(a[0]).localeCompare(String(b[0]))).map(([key, count]) => {
        const start = (cursor / total) * 360;
        cursor += count;
        const end = (cursor / total) * 360;
        return `${colorFn(key)} ${start}deg ${end}deg`;
      });
      return slices.length > 1 ? `conic-gradient(${slices.join(',')})` : (slices[0]?.replace(/ .*/, '') || '#3498db');
    }

    // Cluster bubble icon: collapse nearby station dots into one numbered dot at
    // low zoom, sized/shaded by how many stations it contains (ported from the
    // build_mbon_html.py template's mbonClusterIconFn).
    function mapClusterIcon(cluster) {
      const n = cluster.getChildCount();
      const sizeClass = n < 5 ? 'sm' : (n < 15 ? 'md' : 'lg');
      const dim = n < 5 ? 34 : (n < 15 ? 40 : 48);
      const children = cluster.getAllChildMarkers ? cluster.getAllChildMarkers() : [];
      const entries = mapClusterEntries(children);
      const sampleBg = mapConicSlices(entries, item => `${item.project}::${item.run}::${item.sample}`, mapHashColor);
      const runBg = mapConicSlices(entries, item => `${item.project}::${item.run}`, mapHashColor);
      const projectBg = mapConicSlices(entries, item => item.project, mapHashColor);
      const title = `${n} station(s), ${entries.length} sample-level slice(s)`;
      return L.divIcon({
        html: `<div class="mbon-cluster mbon-cluster-${sizeClass}" title="${mapEsc(title)}" style="--sample-bg:${sampleBg};--run-bg:${runBg};--project-bg:${projectBg}"><i class="mbon-cluster-run-ring"></i><i class="mbon-cluster-project-ring"></i><span>${n}</span></div>`,
        className: 'mbon-cluster-wrap',
        iconSize: L.point(dim, dim),
      });
    }

    function mapEsc(v) {
      return String(v == null ? '' : v)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function mapClusterPopupHtml(cluster) {
      const markers = (cluster.getAllChildMarkers ? cluster.getAllChildMarkers() : [])
        .slice()
        .sort((a, b) => String(a._stn?.station || '').localeCompare(String(b._stn?.station || '')));
      const hierarchy = mapClusterEntries(markers);
      const projectCount = new Set(hierarchy.map(item => item.project)).size;
      const runCount = new Set(hierarchy.map(item => `${item.project}::${item.run}`)).size;
      const rows = markers.slice(0, 40).map((m, i) => {
        const stn = m._stn || {};
        const samples = (stn.samples || []).slice(0, 8);
        const sampleChips = samples.length
          ? `<div class="mbon-cluster-samples">${samples.map(s => {
              const sampleTitle = [s.project_name, s.run_directory || s.run_id, s.alias || s.mbon_data_id].filter(Boolean).join(' > ');
              return `<button type="button" data-mbon-sample="${mapEsc(s.mbon_data_id)}" title="${mapEsc(sampleTitle)}">${mapEsc(String(s.alias || ('#'+s.mbon_data_id)).slice(0,18))}</button>`;
            }).join('')}</div>`
          : '';
        const stationRuns = new Set(samples.map(sample => sample.run_directory || sample.run_id).filter(Boolean));
        const stationProjects = new Set(samples.map(sample => sample.project_name).filter(Boolean));
        const levels = `${stationProjects.size || 1} project(s) · ${stationRuns.size || 1} run(s) · ${(stn.sample_count || samples.length || 0)} sample(s)`;
        return `<div class="mbon-cluster-row" role="button" tabindex="0" data-mbon-station="${mapEsc(mapStationKey(stn))}"><span class="mbon-cluster-swatch" style="background:${stn._type === 'kraken2' ? MARKER_FILL_K2 : MARKER_FILL_DEFAULT}"></span><strong>${mapEsc(stn.station || 'Station '+(i+1))}</strong><small>${mapEsc(stn._type || '')} · ${mapEsc(levels)}</small>${sampleChips}</div>`;
      }).join('');
      const more = markers.length > 40 ? `<div class="mbon-cluster-more">+ ${markers.length - 40} more station(s)</div>` : '';
      return `<div class="mbon-cluster-popup"><div class="mbon-cluster-popup-head"><strong>${markers.length} station(s)</strong><span>${projectCount || 1} project(s) · ${runCount || 1} run(s) · ${hierarchy.length} sample slice(s)</span></div>${rows}${more}</div>`;
    }

    function mapBindClusterPopup(clusterLayer) {
      const root = clusterLayer.getPopup?.().getElement?.()?.querySelector('.mbon-cluster-popup');
      if (!root || root._mbonBound) return;
      root._mbonBound = true;
      const closePopup = () => { try { clusterLayer.closePopup(); } catch(_) {} };
      root.querySelectorAll('[data-mbon-station]').forEach(row => {
        const openStation = async () => {
          const key = row.getAttribute('data-mbon-station');
          if (key) await window.__mbonMapOpenStation?.(key);
          closePopup();
        };
        row.addEventListener('click', e => {
          if (e.target.closest('[data-mbon-sample]')) return;
          openStation();
        });
        row.addEventListener('keydown', e => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openStation(); }
        });
      });
      root.querySelectorAll('[data-mbon-sample]').forEach(btn => {
        btn.addEventListener('click', async e => {
          e.stopPropagation();
          const id = btn.getAttribute('data-mbon-sample');
          if (id) await window.__mbonMapOpenSample?.(id);
          closePopup();
        });
      });
    }

    const MARKER_FILL_DEFAULT   = '#3498db';
    const MARKER_FILL_ACTIVE    = '#f97316';
    const MARKER_FILL_SELECTED  = '#8b5cf6';
    const MARKER_FILL_K2        = '#e07b39';
    const MARKER_STROKE_DEFAULT = '#2c3e50';
    const MARKER_STROKE_ACTIVE  = '#9a3412';

    function mapStationKey(stn) { return `${stn._type}::${stn.station}::${stn.latitude}::${stn.longitude}`; }

    async function loadMapAvailableTypes() {
      try {
        const r = await axios.get(`${API}/map/available-types`);
        mapAvailableTypes.value = r.data;
        // Ensure mapActiveTypes only contains available ones
        mapActiveTypes.value = mapActiveTypes.value.filter(t => r.data.includes(t));
        if (!mapActiveTypes.value.length && r.data.length) mapActiveTypes.value = [r.data[0]];
      } catch { mapAvailableTypes.value = ['wilder', 'kraken2']; }
    }

    function toggleMapType(type) {
      const arr = [...mapActiveTypes.value];
      const idx = arr.indexOf(type);
      if (idx >= 0) arr.splice(idx, 1); else arr.push(type);
      mapActiveTypes.value = arr;
    }

    function mapClearSelection() {
      mapSelectedStations.value = new Set();
      _leafletMarkers.forEach(m => {
        const fill = m._stn?._type === 'kraken2' ? MARKER_FILL_K2 : MARKER_FILL_DEFAULT;
        if (m !== _activeMarker) m.setStyle({ fillColor: fill, color: MARKER_STROKE_DEFAULT, fillOpacity: 0.72, weight: 2 });
      });
    }

    function mapBgClick() { /* clicking map background does not deselect popup */ }

    /* ── Basemap tiles ───────────────────────────────────────────────────────
       Esri World Light Gray Canvas: keyless, no registration, and visually
       close to the CARTO "light_all" basemap this dashboard was designed for.
       (CARTO's public basemap CDN now rejects unauthenticated requests with
       "API key required", which is why we no longer point at it.)
       Land/water is one layer, place labels are a second reference layer.
       If Esri tiles fail (offline, blocked), we fall back to standard OSM
       tiles automatically so the map is never blank.                         */
    const MBON_TILES = {
      base: {
        url: 'https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}',
        options: {
          attribution: 'Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ',
          minZoom: 0, maxZoom: 19, maxNativeZoom: 16,
        },
      },
      labels: {
        url: 'https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}',
        options: { minZoom: 0, maxZoom: 19, maxNativeZoom: 16, pane: 'mbonLabels' },
      },
      fallback: {
        url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
        options: {
          attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
          minZoom: 0, maxZoom: 19,
        },
      },
    };

    // Adds the base + label layers to a Leaflet map, wired with a one-shot
    // fallback to OSM if the primary tile service errors out.
    function mapAddBaseLayers(map) {
      if (!map) return;
      // Dedicated pane so place labels draw above the base tiles but below
      // markers/overlays.
      if (!map.getPane('mbonLabels')) {
        map.createPane('mbonLabels');
        map.getPane('mbonLabels').style.zIndex = 350;
        map.getPane('mbonLabels').style.pointerEvents = 'none';
      }
      let switched = false;
      const base = L.tileLayer(MBON_TILES.base.url, MBON_TILES.base.options).addTo(map);
      const labels = L.tileLayer(MBON_TILES.labels.url, MBON_TILES.labels.options).addTo(map);
      let errors = 0;
      base.on('tileerror', () => {
        // A couple of missing tiles at the edge of coverage is normal; only
        // swap basemaps when the service is clearly unavailable.
        if (switched || ++errors < 4) return;
        switched = true;
        try { map.removeLayer(base); } catch (_) {}
        try { map.removeLayer(labels); } catch (_) {}
        L.tileLayer(MBON_TILES.fallback.url, MBON_TILES.fallback.options).addTo(map);
      });
      return base;
    }

    function mapEnsureLeaflet() {
      if (_leafletMap) { _leafletMap.remove(); _leafletMap = null; _leafletMarkers = []; _activeMarker = null; }
      _leafletMap = L.map('mbon-leaflet-map', { center: [38.8, -74.6], zoom: 9, preferCanvas: true });
      mapAddBaseLayers(_leafletMap);
      // ResizeObserver fires once the container gets real dimensions (flex layout settles)
      const container = document.getElementById('mbon-leaflet-map');
      const ro = new ResizeObserver((entries, obs) => {
        const h = entries[0]?.contentRect?.height || 0;
        if (h > 0 && _leafletMap) {
          _leafletMap.invalidateSize();
          obs.disconnect();
        }
      });
      ro.observe(container);
    }

    function mapClearMarkers() {
      if (_leafletClusterGroup) {
        try { _leafletClusterGroup.clearLayers(); } catch(_) {}
        try { _leafletMap && _leafletMap.removeLayer(_leafletClusterGroup); } catch(_) {}
        _leafletClusterGroup = null;
      }
      _leafletMarkers.forEach(m => {
        try { m.remove(); } catch(_) {}
      });
      _leafletMarkers = []; _activeMarker = null;
    }

    function mapClearActive() {
      if (_activeMarker) {
        const fill = _activeMarker._stn?._type === 'kraken2' ? MARKER_FILL_K2 : MARKER_FILL_DEFAULT;
        _activeMarker.setStyle({ color: MARKER_STROKE_DEFAULT, fillColor: fill, fillOpacity: 0.72, weight: 2 });
        _activeMarker = null;
      }
    }

    function mapActivateMarker(marker) {
      mapClearActive();
      _activeMarker = marker;
      marker.setStyle({ color: MARKER_STROKE_ACTIVE, fillColor: MARKER_FILL_ACTIVE, fillOpacity: 0.95, weight: 3 });
      marker.bringToFront();
    }

    function mapRadiusScale(stations) {
      const reads = stations.map(s => +s.total_reads || 0);
      const maxR = Math.max(...reads, 1);
      return r => Math.max(6, Math.sqrt(r / maxR) * 36);
    }

    async function loadMapK2Groups() {
      try {
        const r = await axios.get(`${API}/map/groups/kraken2`);
        mapK2Groups.value = Array.isArray(r.data) ? r.data : [];
      } catch (_) { mapK2Groups.value = []; }
    }

    async function loadMapStations() {
      mapLoading.value = true;
      mapActiveStation.value = null;
      mapStations.value = [];
      mapClearMarkers();

      const results = [];
      const fetches = [];
      if (mapActiveTypes.value.includes('wilder')) {
        const p = new URLSearchParams();
        if (mapWilderJob.value) p.set('wilder_job_id', mapWilderJob.value);
        if (mapWilderGroup.value) p.set('organism_group', mapWilderGroup.value);
        fetches.push(axios.get(`${API}/map/stations/wilder?${p}`)
          .then(r => r.data.forEach(s => { s._type = 'wilder'; results.push(s); }))
          .catch(() => {}));
      }
      if (mapActiveTypes.value.includes('kraken2')) {
        // No rank filter: load all ranks so popup dropdown can filter client-side.
        // Optional organism-group filter (derived from taxonomy lineage server-side).
        const pk = new URLSearchParams();
        if (mapK2Group.value) pk.set('group', mapK2Group.value);
        fetches.push(axios.get(`${API}/map/stations/kraken2?${pk}`)
          .then(r => r.data.forEach(s => { s._type = 'kraken2'; results.push(s); }))
          .catch(() => {}));
      }
      await Promise.all(fetches);
      mapStations.value = results.filter(s => s.latitude && s.longitude);
      mapLoading.value = false;

      await nextTick();
      if (mapLeafletReady()) {
        _leafletMap.invalidateSize();
        mapRenderMarkers();
      }
    }

    function mapLeafletReady() {
      if (!_leafletMap) return false;
      try { 
        return document.contains(_leafletMap.getContainer()); 
      } catch(_) { 
        return false; 
      }
    }

    function mapRenderMarkers() {
      if (!mapLeafletReady()) return;
      mapClearMarkers();
      const stations = filteredMapStations.value;
      if (!stations.length) return;
      const radiusFn = mapRadiusScale(stations);
      const bounds = [];
      // Cluster group: nearby dots collapse into one numbered bubble at low zoom.
      // Fall back to plain markers if the plugin failed to load.
      const useCluster = typeof L.markerClusterGroup === 'function';
      if (!useCluster) {
        // Root cause of "always single dots": the markercluster plugin didn't
        // load, so we silently fell back to plain markers. Surface it loudly.
        console.warn('[MBON] leaflet.markercluster is NOT loaded — dots will not merge into numbered clusters. Check that the markercluster <script> loaded (network tab).');
      }
      if (useCluster) {
        _leafletClusterGroup = L.markerClusterGroup({
          showCoverageOnHover: false,
          maxClusterRadius: 60,
          spiderfyDistanceMultiplier: 1.35,
          spiderfyOnMaxZoom: true,
          zoomToBoundsOnClick: true,   // clicking a cluster zooms into its stations
          iconCreateFunction: mapClusterIcon,
        }).addTo(_leafletMap);
        _leafletClusterGroup.on('clustermouseover', e => {
          try { e.layer.bindTooltip(`${e.layer.getChildCount()} station(s)`, { sticky: true }).openTooltip(); } catch(_) {}
        });
        _leafletClusterGroup.on('clusterclick', e => {
          try {
            e.layer.bindPopup(mapClusterPopupHtml(e.layer), { maxWidth: 360 }).openPopup();
            setTimeout(() => mapBindClusterPopup(e.layer), 0);
          } catch(_) {}
        });
      }
      const addToMap = (m) => { if (useCluster) _leafletClusterGroup.addLayer(m); else m.addTo(_leafletMap); };
      stations.forEach(stn => {
        const lat = +stn.latitude, lon = +stn.longitude;
        if (!lat && !lon) return;
        const defaultFill = stn._type === 'kraken2' ? MARKER_FILL_K2 : MARKER_FILL_DEFAULT;
        const _sc = +stn.sample_count || (stn.samples ? stn.samples.length : 0);
        const marker = L.circleMarker([lat, lon], {
          radius: radiusFn(+stn.total_reads || 0),
          color: MARKER_STROKE_DEFAULT, fillColor: defaultFill, fillOpacity: 0.72, weight: 2,
        });
        marker._stn = stn;
        marker.bindTooltip(
          `${stn.station || '(' + lat.toFixed(3) + ', ' + lon.toFixed(3) + ')'} <span style="opacity:.7">(${stn._type})</span>`
            + (_sc > 1 ? ` <span style="opacity:.7">· ${_sc} samples</span>` : ''),
          { permanent: false, className: 'mbon-marker-label', direction: 'top', offset: [0, -4] }
        );
        addToMap(marker);
        marker.on('click', async (e) => {
          const key = mapStationKey(stn);
          if (e.originalEvent.ctrlKey || e.originalEvent.metaKey) {
            // Multi-select: toggle in selection set
            const sel = new Set(mapSelectedStations.value);
            if (sel.has(key)) {
              sel.delete(key);
              marker.setStyle({ fillColor: defaultFill, color: MARKER_STROKE_DEFAULT, fillOpacity: 0.72, weight: 2 });
            } else {
              sel.add(key);
              marker.setStyle({ fillColor: MARKER_FILL_SELECTED, color: '#5b21b6', fillOpacity: 0.9, weight: 2.5 });
            }
            mapSelectedStations.value = sel;
            return;
          }
          // Normal click: activate station
          mapActivateMarker(marker);
          mapActiveStation.value = stn;
          mapSamplesOpen.value = false;
          mapStnSearch.value = '';
          // Reset popup filters for the new station
          mapPopupK2Rank.value = chooseRank(stn.rank_options || (stn.top_taxa || []).map(x => x.rank), mapPopupK2Rank.value);
          mapPopupWilderView.value = 'species';
          mapPopupTab.value = 'distribution';
          mapStnHighlighted.value = new Set();
          // Reset sample-merge state for the new station
          mapMergeSelected.value = new Set();
          mapMergeResult.value = null;
          mapMergeError.value = '';
          mapMergeView.value = 'combined';
          tableSorts.value = { ...tableSorts.value, mapstn: { key: 'total_reads', dir: 'desc' } };
          await nextTick();
          await nextTick(); // second tick ensures SVG refs are mounted
          drawMapPopupChart();
        });
        bounds.push([lat, lon]);
        _leafletMarkers.push(marker);
      });
      if (bounds.length === 1) _leafletMap.setView(bounds[0], 11);
      else if (bounds.length > 1) _leafletMap.fitBounds(bounds, { padding: [40, 40] });
    }

    function drawMapPopupChart() {
      if (!mapPopupChartSvg.value || !mapPopupChartWrap.value || !mapActiveStation.value) return;
      if (mapPopupTab.value === 'compare') { drawMapPopupStationCompare(); return; }
      if (mapPopupTab.value === 'depth') { drawMapPopupDepth(); return; }
      // Merged (cross-sample) views take over the chart when a merge is active.
      if (mapMergeResult.value) {
        if (mapMergeView.value === 'occupancy') drawMapOccupancy();
        else if (mapMergeView.value === 'heatmap') drawMapHeatmap();
        else drawMapMergedChart();
        return;
      }
      const n = mapTopN.value;
      const stn = mapActiveStation.value;
      const hl = mapStnHighlighted.value;

      if (stn._type === 'wilder') {
        if (mapPopupWilderView.value === 'groups') {
          // Grouped view: one bar per organism_group
          let data = mapPopupGroupedData.value;
          if (hl.size > 0) data = data.filter(d => hl.has(d.organism_group));
          else data = data.slice(0, n);
          const mGTotal = d3.sum(data, d => +d.total_reads) || 1;
          drawBar('mapPopup', mapPopupChartSvg.value, mapPopupChartWrap.value, data, {
            labelKey: 'organism_group', valueKey: 'total_reads',
            colorFn: d => groupColor(d.organism_group),
            fmtVal: v => `${d3.format(',.0f')(v)} (${(v/mGTotal*100).toFixed(1)}%)`,
            fmtTip: d => `<strong>${d.organism_group}</strong>Total reads: ${Number(d.total_reads).toLocaleString()}<br>Species: ${d.species_count}`,
          });
        } else if (mapPopupWilderView.value === 'samples') {
          // Per-sample view: one bar per sample alias
          let data = (stn.samples || []).map(s => ({ alias: s.alias, total_reads: s.total_reads }));
          if (hl.size > 0) data = data.filter(d => hl.has(d.alias));
          const mSmpTotal = d3.sum(data, d => +d.total_reads) || 1;
          drawBar('mapPopup', mapPopupChartSvg.value, mapPopupChartWrap.value, data, {
            labelKey: 'alias', valueKey: 'total_reads',
            colorFn: (_, i) => d3.schemeTableau10[i % 10],
            fmtVal: v => `${d3.format(',.0f')(v)} (${(v/mSmpTotal*100).toFixed(1)}%)`,
            fmtTip: d => `<strong>${d.alias}</strong>Reads: ${Number(d.total_reads).toLocaleString()}`,
          });
        } else {
          // Species view: one bar per scientific_name
          let data = [...filteredSortedMapSpecies.value].sort((a, b) => +b.total_reads - +a.total_reads);
          if (hl.size > 0) data = data.filter(d => hl.has(d.scientific_name));
          else data = data.slice(0, n);
          const mSTotal = d3.sum(data, d => +d.total_reads) || 1;
          drawBar('mapPopup', mapPopupChartSvg.value, mapPopupChartWrap.value, data, {
            labelKey: 'scientific_name', valueKey: 'total_reads',
            colorFn: d => groupColor(d.organism_group),
            fmtVal: v => `${d3.format(',.0f')(v)} (${(v/mSTotal*100).toFixed(1)}%)`, italic: true,
            fmtTip: d => `<strong><em>${d.scientific_name}</em></strong>${d.common_name ? d.common_name + '<br>' : ''}Group: ${d.organism_group || '–'}<br>Reads: ${Number(d.total_reads).toLocaleString()}`,
          });
        }
      } else {
        // Kraken2: filter by rank then show top N (or only highlighted)
        let data = [...filteredSortedMapTaxa.value].sort((a, b) => +b.total_reads - +a.total_reads);
        if (hl.size > 0) data = data.filter(d => hl.has(d.name));
        else data = data.slice(0, n);
        const mKTotal = d3.sum(data, d => +d.total_reads) || 1;
        drawBar('mapPopup', mapPopupChartSvg.value, mapPopupChartWrap.value, data, {
          labelKey: 'name', valueKey: 'total_reads',
          colorFn: d => rankColor(d.rank),
          fmtVal: v => `${d3.format(',.0f')(v)} (${(v/mKTotal*100).toFixed(1)}%)`, italic: true,
          fmtTip: d => `<strong><em>${d.name}</em></strong>Rank: ${rankLabel(d.rank)}<br>Reads: ${Number(d.total_reads).toLocaleString()}<br>Avg abun: ${Number(d.avg_abundance || 0).toFixed(2)}%`,
        });
      }
    }

    function mapSetPopupTab(tab) {
      mapPopupTab.value = tab;
      nextTick(drawMapPopupChart);
    }

    function drawMapPopupStationCompare() {
      const stn = mapActiveStation.value;
      if (!stn) return;
      if (mapMergeResult.value && (mapMergeResult.value.matrix || []).length && (mapMergeResult.value.samples || []).length) {
        drawMapHeatmap();
        return;
      }
      if (stn._type === 'wilder') {
        const samples = stn.samples || [];
        const taxaMap = {};
        samples.forEach((sample, idx) => (sample.species || []).forEach(item => {
          const name = item.scientific_name;
          if (!name) return;
          if (!taxaMap[name]) taxaMap[name] = { name, total: 0, group: item.organism_group };
          const v = +(item.total_reads || 0);
          taxaMap[name][`s${idx}`] = (taxaMap[name][`s${idx}`] || 0) + v;
          taxaMap[name].total += v;
        }));
        const rows = Object.values(taxaMap).sort((a,b)=>b.total-a.total).slice(0,mapTopN.value);
        drawGroupedBar(mapPopupChartSvg.value, mapPopupChartWrap.value, null, rows, samples.map((_,i)=>i), {
          labelKey:'name', valuePrefix:'s', valueSuffix:'', colorFn:(id,gi)=>d3.schemeTableau10[gi%10],
          labelFn:id=>samples[id]?.alias || `Sample ${id+1}`,
          fmtVal:v=>d3.format(',.0f')(v),
          fmtTip:(row,id,val)=>`<strong><em>${row.name}</em></strong><br>Sample: ${samples[id]?.alias || id}<br>Reads: ${Number(val).toLocaleString()}`,
        });
      } else {
        const rows = [...filteredSortedMapTaxa.value].sort((a,b)=>+b.total_reads-+a.total_reads).slice(0,mapTopN.value);
        drawBar('mapPopup', mapPopupChartSvg.value, mapPopupChartWrap.value, rows, {
          labelKey:'name', valueKey:'avg_abundance', colorFn:d=>rankColor(d.rank), pct:true, italic:true,
          fmtVal:v=>Number(v).toFixed(2)+'%',
          fmtTip:d=>`<strong><em>${d.name}</em></strong>Rank: ${rankLabel(d.rank)}<br>Avg abun: ${Number(d.avg_abundance||0).toFixed(3)}%<br>Reads: ${Number(d.total_reads||0).toLocaleString()}`,
        });
      }
    }

    function drawMapPopupDepth() {
      const stn = mapActiveStation.value;
      if (!stn) return;
      const rows = (stn.samples || [])
        .filter(s => s.sample_depth != null && s.total_reads != null)
        .map(s => ({ alias: s.alias || ('#' + s.mbon_data_id), sample_depth: +s.sample_depth, total_reads: +s.total_reads || 0 }))
        .sort((a,b)=>a.sample_depth-b.sample_depth);
      if (!rows.length) {
        const svg=d3.select(mapPopupChartSvg.value).attr('width', mapPopupChartWrap.value.clientWidth || 360).attr('height', 90);
        svg.selectAll('*').remove();
        svg.append('text').attr('x', 16).attr('y', 44).attr('fill', '#64748b').attr('font-size', 12).text('No sample depth values are available for this station.');
        return;
      }
      drawBar('mapPopup', mapPopupChartSvg.value, mapPopupChartWrap.value, rows, {
        labelKey:'alias', valueKey:'total_reads', colorFn:(_,i)=>d3.schemeTableau10[i%10],
        fmtVal:v=>d3.format(',.0f')(v),
        fmtTip:d=>`<strong>${d.alias}</strong><br>Depth: ${d.sample_depth} m<br>Reads: ${Number(d.total_reads).toLocaleString()}`,
      });
    }

    function toggleMapStnHighlight(key) {
      const s = new Set(mapStnHighlighted.value);
      if (s.has(key)) s.delete(key); else s.add(key);
      mapStnHighlighted.value = s;
      drawMapPopupChart();
    }
    function clearMapStnHighlights() {
      mapStnHighlighted.value = new Set();
      drawMapPopupChart();
    }

    // ── Map station compare ───────────────────────────────────────────
    async function toggleMapCompare() {
      mapCompareOpen.value = !mapCompareOpen.value;
      if (mapCompareOpen.value) { await nextTick(); drawMapCompare(); }
    }

    function drawMapCompare() {
      const stns = selectedStationObjects.value;
      if (!stns.length || !mapCompareSvg.value || !mapCompareWrap.value) return;

      const isWilder = stns[0]._type === 'wilder';
      const topN = mapCompareTopN.value;
      const grpFilter = mapCompareGroup.value;
      const rkFilter  = mapCompareK2Rank.value;

      // Build unified taxon map: name -> { name, group/rank, s0, s1, ..., total }
      const taxaMap = {};
      stns.forEach((stn, i) => {
        let items = isWilder ? (stn.top_species || []) : (stn.top_taxa || []);
        if (isWilder && grpFilter) items = items.filter(x => x.organism_group === grpFilter);
        if (!isWilder && rkFilter) items = items.filter(x => x.rank === rkFilter);
        items.forEach(item => {
          const name = isWilder ? item.scientific_name : item.name;
          if (!name) return;
          if (!taxaMap[name]) taxaMap[name] = { name, group: item.organism_group, rank: item.rank, total: 0 };
          const v = +(item.total_reads || 0);
          taxaMap[name][`s${i}`] = (taxaMap[name][`s${i}`] || 0) + v;
          taxaMap[name].total += v;
        });
      });

      const ids = stns.map((_, i) => i);
      const labelFn = id => stns[id]?.station || `Station ${id}`;

      if (mapCompareView.value === 'aggregate') {
        // Single aggregate bar chart
        const data = Object.values(taxaMap)
          .sort((a, b) => b.total - a.total)
          .slice(0, topN);
        const total = d3.sum(data, d => d.total) || 1;
        drawHBar(mapCompareSvg.value, mapCompareWrap.value, data, {
          labelKey: 'name',
          valueKey: 'total',
          colorFn: d => isWilder ? groupColor(d.group) : rankColor(d.rank),
          fmtVal: v => `${d3.format(',.0f')(v)} (${(v / total * 100).toFixed(1)}%)`,
          italic: isWilder || !isWilder, // italic species/taxon names
          fmtTip: d => `<strong><em>${d.name}</em></strong>${d.group ? '<br>Group: ' + d.group : ''}${d.rank ? '<br>Rank: ' + rankLabel(d.rank) : ''}<br>Total reads: ${Number(d.total).toLocaleString()}`,
        });
        if (mapCompareLegend.value) mapCompareLegend.value.innerHTML = '';
      } else if (mapCompareView.value === 'persample') {
        // Per-sample grouped bar: flatten all samples from all selected stations
        const allSamples = stns.flatMap(stn => (stn.samples || []));
        if (!allSamples.length) return;
        const sampleTaxaMap = {};
        allSamples.forEach((samp, i) => {
          let items = samp.species || [];
          if (grpFilter) items = items.filter(x => x.organism_group === grpFilter);
          items.forEach(item => {
            const name = item.scientific_name;
            if (!name) return;
            if (!sampleTaxaMap[name]) sampleTaxaMap[name] = { name, group: item.organism_group, rank: item.taxonomic_rank, total: 0 };
            const v = +(item.total_reads || 0);
            sampleTaxaMap[name][`s${i}`] = (sampleTaxaMap[name][`s${i}`] || 0) + v;
            sampleTaxaMap[name].total += v;
          });
        });
        const sampleIds = allSamples.map((_, i) => i);
        const sampleLabelFn = id => allSamples[id]?.alias || `Sample ${id}`;
        const matrix = Object.values(sampleTaxaMap)
          .sort((a, b) => b.total - a.total)
          .slice(0, topN);
        drawGroupedBar(mapCompareSvg.value, mapCompareWrap.value, mapCompareLegend.value, matrix, sampleIds, {
          labelKey: 'name',
          valuePrefix: 's',
          valueSuffix: '',
          colorFn: (id, gi) => d3.schemeTableau10[gi % 10],
          labelFn: sampleLabelFn,
          fmtVal: v => d3.format(',.0f')(v),
          fmtTip: (row, id, val) => `<strong><em>${row.name}</em></strong><br>Sample: ${sampleLabelFn(id)}<br>Reads: ${Number(val).toLocaleString()}`,
        });
      } else {
        // Per-station grouped bar chart
        const matrix = Object.values(taxaMap)
          .sort((a, b) => b.total - a.total)
          .slice(0, topN);
        drawGroupedBar(mapCompareSvg.value, mapCompareWrap.value, mapCompareLegend.value, matrix, ids, {
          labelKey: 'name',
          valuePrefix: 's',
          valueSuffix: '',
          colorFn: (id, gi) => d3.schemeTableau10[gi % 10],
          labelFn,
          fmtVal: v => d3.format(',.0f')(v),
          fmtTip: (row, id, val) => `<strong><em>${row.name}</em></strong><br>Station: ${labelFn(id)}<br>Reads: ${Number(val).toLocaleString()}`,
        });
      }
    }

    // Kraken2 map popup: click a taxon row to load cross-compare panel
    function mapK2TaxonClick(name) {
      loadK2CrossCompare(name);
    }

    // Navigate to Kraken2 tab and pre-load for the active station
    async function mapOpenInK2(target) {
      const stn = mapActiveStation.value;
      if (!stn || stn._type !== 'kraken2') return;
      await nav('kraken2');
      if (target === 'sunburst') {
        // Find first report matching this station and load its sunburst
        const sample = (stn.samples || [])[0];
        if (sample?.report_id) {
          k2SbPanels.value[0].report = sample.report_id;
          if (k2Tab.value !== 'sunburst') k2Tab.value = 'sunburst';
          await nextTick();
          loadSbPanel(k2SbPanels.value[0]);
        }
      } else {
        // LCA tab — load reports and switch to browser
        k2Tab.value = 'browser';
      }
    }

    // Navigate to Wilderlab tab and pre-filter for the active station
    async function mapOpenInWilder() {
      const stn = mapActiveStation.value;
      if (!stn || stn._type !== 'wilder') return;
      await nav('wilder');
      wilderTab.value = 'results';
      await nextTick();
      loadWilderResults();
    }

    // Click a sample in the samples popover → navigate and filter
    async function mapFilterBySample(s) {
      mapSamplesOpen.value = false;
      const stn = mapActiveStation.value;
      if (!stn) return;
      if (stn._type === 'wilder') {
        await nav('wilder');
        wilderTab.value = 'results';
        wilderJobFilter.value = s.job_id || '';
        await nextTick();
        loadWilderResults();
      } else {
        // Kraken2: navigate to kraken2 tab and select the report
        await nav('kraken2');
        if (s.report_id) {
          k2SelectedReport.value = s.report_id;
          k2Tab.value = 'browser';
          await nextTick();
          loadK2LCAs();
        }
      }
    }

    window.__mbonMapOpenStation = async (key) => {
      const stn = mapStations.value.find(s => mapStationKey(s) === key);
      if (!stn) return;
      mapActiveStation.value = stn;
      mapSamplesOpen.value = true;
      mapStnSearch.value = '';
      mapPopupK2Rank.value = chooseRank(stn.rank_options || (stn.top_taxa || []).map(x => x.rank), mapPopupK2Rank.value);
      mapPopupWilderView.value = 'species';
      mapPopupTab.value = 'distribution';
      mapStnHighlighted.value = new Set();
      await nextTick();
      drawMapPopupChart();
    };
    window.__mbonMapOpenSample = async (id) => {
      const sampleId = Number(id);
      for (const stn of mapStations.value) {
        const sample = (stn.samples || []).find(s => Number(s.mbon_data_id) === sampleId);
        if (sample) {
          await window.__mbonMapOpenStation(mapStationKey(stn));
          await mapFilterBySample(sample);
          return;
        }
      }
    };

    // ── Sample merging (cross-sample) ─────────────────────────────────
    // Toggle a sample into/out of the merge selection.
    function mapToggleMergeSample(s) {
      const id = s && s.mbon_data_id;
      if (id == null) return;
      const sel = new Set(mapMergeSelected.value);
      if (sel.has(id)) sel.delete(id); else sel.add(id);
      mapMergeSelected.value = sel;
      // If a merge was already computed, invalidate it until re-run.
      if (mapMergeResult.value) { mapMergeResult.value = null; }
    }
    function mapMergeSelectAll() {
      const stn = mapActiveStation.value;
      if (!stn) return;
      mapMergeSelected.value = new Set((stn.samples || []).map(s => s.mbon_data_id).filter(v => v != null));
      mapMergeResult.value = null;
    }
    function mapClearMerge() {
      mapMergeSelected.value = new Set();
      mapMergeResult.value = null;
      mapMergeError.value = '';
      nextTick(drawMapPopupChart);
    }
    async function mapMergeSamples() {
      const stn = mapActiveStation.value;
      if (!stn) return;
      const ids = [...mapMergeSelected.value];
      if (ids.length < 1) { mapMergeError.value = 'Select at least one sample to merge.'; return; }
      mapMergeLoading.value = true;
      mapMergeError.value = '';
      try {
        const body = {
          type: stn._type,
          sample_ids: ids,
          top_n: 200,
        };
        if (stn._type === 'kraken2' && mapPopupK2Rank.value) body.rank = mapPopupK2Rank.value;
        const r = await axios.post(`${API}/map/merged-distribution`, body);
        mapMergeResult.value = r.data;
        mapSamplesOpen.value = false;
        await nextTick(); await nextTick();
        drawMapPopupChart();
      } catch (e) {
        mapMergeError.value = (e && e.response && e.response.data && e.response.data.error) || 'Merge failed.';
        mapMergeResult.value = null;
      } finally {
        mapMergeLoading.value = false;
      }
    }

    // Cross-sample occupancy: tag taxa core / shared / unique across merged samples.
    function mapMergeTagFor(sampleCount) {
      const n = mapMergeResult.value ? (mapMergeResult.value.sample_count || 0) : 0;
      if (!n || n < 2) return null;
      if (sampleCount >= n) return { cls: 'core', label: 'core' };
      if (sampleCount <= 1) return { cls: 'unique', label: 'unique' };
      return { cls: 'shared', label: 'shared' };
    }

    // Draw the merged combined distribution into the popup chart area.
    function drawMapMergedChart() {
      const res = mapMergeResult.value;
      if (!res || !mapPopupChartSvg.value || !mapPopupChartWrap.value) return;
      const stn = mapActiveStation.value;
      const isWilder = res.type === 'wilder';
      const n = mapTopN.value;
      let rows = (res.taxa || []).slice();
      // Search filter reuses the popup search box.
      const q = (mapStnSearch.value || '').toLowerCase();
      if (q) {
        rows = rows.filter(d => {
          const nm = isWilder ? (d.scientific_name || '') : (d.name || '');
          const grp = (d.organism_group || d.rank || '');
          return nm.toLowerCase().includes(q) || String(grp).toLowerCase().includes(q);
        });
      }
      rows = rows.sort((a, b) => +b.total_reads - +a.total_reads).slice(0, n);
      const total = res.total_reads || d3.sum(rows, d => +d.total_reads) || 1;
      if (isWilder) {
        drawBar('mapPopup', mapPopupChartSvg.value, mapPopupChartWrap.value, rows, {
          labelKey: 'scientific_name', valueKey: 'total_reads',
          colorFn: d => groupColor(d.organism_group),
          fmtVal: v => `${d3.format(',.0f')(v)} (${(v / total * 100).toFixed(1)}%)`, italic: true,
          fmtTip: d => `<strong><em>${d.scientific_name}</em></strong>${d.common_name ? d.common_name + '<br>' : ''}Group: ${d.organism_group || '–'}<br>Reads: ${Number(d.total_reads).toLocaleString()}<br>In ${d.sample_count}/${res.sample_count} merged sample(s)`,
        });
      } else {
        drawBar('mapPopup', mapPopupChartSvg.value, mapPopupChartWrap.value, rows, {
          labelKey: 'name', valueKey: 'total_reads',
          colorFn: d => rankColor(d.rank),
          fmtVal: v => `${d3.format(',.0f')(v)} (${(v / total * 100).toFixed(1)}%)`, italic: true,
          fmtTip: d => `<strong><em>${d.name}</em></strong>Rank: ${rankLabel(d.rank)}<br>Reads: ${Number(d.total_reads).toLocaleString()}<br>In ${d.sample_count}/${res.sample_count} merged sample(s)`,
        });
      }
    }

    // Cross-Sample: switch merged sub-view (combined / occupancy / heatmap).
    function mapSetMergeView(v) {
      mapMergeView.value = v;
      nextTick(() => { nextTick(drawMapPopupChart); });
    }

    // Occupancy bar: how many taxa are detected in exactly k of the merged
    // samples (core = all, unique = one) — the template's uniqueness profile.
    function drawMapOccupancy() {
      const res = mapMergeResult.value;
      if (!res || !mapPopupChartSvg.value || !mapPopupChartWrap.value) return;
      const N = res.sample_count || 0;
      const bins = {};
      (res.taxa || []).forEach(t => { const k = t.sample_count || 0; if (k > 0) bins[k] = (bins[k] || 0) + 1; });
      const data = [];
      for (let k = 1; k <= N; k++) data.push({ label: 'in ' + k, k, count: bins[k] || 0 });
      drawBar('mapPopup', mapPopupChartSvg.value, mapPopupChartWrap.value, data, {
        labelKey: 'label', valueKey: 'count',
        colorFn: d => d.k >= N ? '#1b7a43' : (d.k <= 1 ? '#a23030' : '#9a6b16'),
        fmtVal: v => `${d3.format(',.0f')(v)} taxa`,
        fmtTip: d => `<strong>Detected in ${d.k} of ${N} sample(s)</strong><br>${d.count} taxa · ${d.k >= N ? 'core' : (d.k <= 1 ? 'unique' : 'shared')}`,
      });
    }

    // Organism × sample heatmap for the merged cohort (reads per cell).
    function drawMapHeatmap() {
      const res = mapMergeResult.value;
      if (!res || !mapPopupChartSvg.value || !mapPopupChartWrap.value) return;
      const svg = d3.select(mapPopupChartSvg.value);
      svg.selectAll('*').remove();
      const samples = res.samples || [];
      let rowsAll = (res.matrix || []).slice();
      const q = (mapStnSearch.value || '').toLowerCase();
      if (q) rowsAll = rowsAll.filter(m => (m.taxon || '').toLowerCase().includes(q));
      const rows = rowsAll.slice(0, Math.min(rowsAll.length, mapTopN.value));
      if (!rows.length || !samples.length) { svg.attr('width', 10).attr('height', 10); return; }
      const wrapW = mapPopupChartWrap.value.clientWidth || 360;
      const margin = { top: 26, right: 10, bottom: 8, left: 132 };
      const cellH = 16;
      const gridW = Math.max(120, wrapW - margin.left - margin.right);
      const cellW = Math.max(14, Math.min(60, gridW / samples.length));
      const width = margin.left + cellW * samples.length + margin.right;
      const height = margin.top + cellH * rows.length + margin.bottom;
      svg.attr('width', width).attr('height', height);
      let maxVal = 0;
      rows.forEach(m => samples.forEach(s => { const v = m.per_sample[String(s.mbon_data_id)] || 0; if (v > maxVal) maxVal = v; }));
      maxVal = maxVal || 1;
      const color = d3.scaleSequential(d3.interpolateBlues).domain([0, Math.sqrt(maxVal)]);
      // column (sample) headers, rotated
      svg.append('g').selectAll('text.col').data(samples).enter().append('text')
        .attr('x', (s, i) => margin.left + i * cellW + cellW / 2)
        .attr('y', margin.top - 8)
        .attr('text-anchor', 'start')
        .attr('transform', (s, i) => `rotate(-40 ${margin.left + i * cellW + cellW / 2} ${margin.top - 8})`)
        .style('font-size', '9px').style('fill', '#456')
        .text(s => { const a = s.alias || ('#' + s.mbon_data_id); return a.length > 10 ? a.slice(0, 10) + '…' : a; });
      const g = svg.append('g');
      rows.forEach((m, ri) => {
        g.append('text').attr('x', margin.left - 6).attr('y', margin.top + ri * cellH + cellH * 0.72)
          .attr('text-anchor', 'end').style('font-size', '9px').style('font-style', 'italic').style('fill', '#333')
          .text(m.taxon && m.taxon.length > 22 ? m.taxon.slice(0, 22) + '…' : (m.taxon || ''));
        samples.forEach((s, ci) => {
          const v = m.per_sample[String(s.mbon_data_id)] || 0;
          g.append('rect')
            .attr('x', margin.left + ci * cellW).attr('y', margin.top + ri * cellH)
            .attr('width', cellW - 1).attr('height', cellH - 1)
            .attr('rx', 2)
            .attr('fill', v > 0 ? color(Math.sqrt(v)) : '#f1f4f7')
            .append('title').text(`${m.taxon} · ${s.alias || ('#' + s.mbon_data_id)}: ${v.toLocaleString()} reads`);
        });
      });
    }

    // Merged taxa filtered+sorted for the table (respects popup search).
    const mapMergedTableRows = computed(() => {
      const res = mapMergeResult.value;
      if (!res) return [];
      const isWilder = res.type === 'wilder';
      let rows = (res.taxa || []).slice();
      const q = (mapStnSearch.value || '').toLowerCase();
      if (q) {
        rows = rows.filter(d => {
          const nm = isWilder ? (d.scientific_name || '') : (d.name || '');
          const grp = (d.organism_group || d.rank || '');
          return nm.toLowerCase().includes(q) || String(grp).toLowerCase().includes(q);
        });
      }
      return applySort(tableSorts.value['mapmerge'] || { key: 'total_reads', dir: 'desc' }, rows);
    });

    // Watch search / rank / size in popup → redraw chart
    watch(mapStnSearch, () => { if (mapActiveStation.value) nextTick(drawMapPopupChart); });
    watch([mapPopupK2Rank, mapPopupWilderView], () => { if (mapActiveStation.value) { mapStnHighlighted.value = new Set(); nextTick(drawMapPopupChart); } });
    watch(mapPopupTab, () => { if (mapActiveStation.value) nextTick(drawMapPopupChart); });
    watch(mapPopupK2RankOptions, opts => {
      const next = chooseRank(opts, mapPopupK2Rank.value);
      if (next !== mapPopupK2Rank.value) mapPopupK2Rank.value = next;
    });
    watch(mapCompareK2RankOptions, opts => {
      const next = chooseRank(opts, mapCompareK2Rank.value);
      if (next !== mapCompareK2Rank.value) mapCompareK2Rank.value = next;
    });
    let _chartResizeTimer = null;
    watch(mapPopupSize, () => {
      if (!mapActiveStation.value) return;
      clearTimeout(_chartResizeTimer);
      _chartResizeTimer = setTimeout(() => nextTick(drawMapPopupChart), 80);
    }, { deep: true });

    // ── Navigation ────────────────────────────────────────────────────
    async function nav(v){
      view.value=v;
      if(v==='collections') { await loadCollections(); await loadProjects(); }
      if(v==='runs')    { await loadRuns(); if(!collections.value.length)loadCollections(); }
      if(v==='samples') { await loadSamples(); if(!collections.value.length)loadCollections(); }
      if(v==='kraken2') { await loadK2Reports(); await loadK2RankOptions(); }
      if(v==='mbon')    { await loadMbon(); await loadMbonFilters(); }
      if(v==='wilder')  { await Promise.all([loadWilderJobs(),loadWilderGroups(),loadWilderSbSamples()]); await loadWilderTopTaxa(); }
      if(v==='explore'){
        // Merged Explore & Map view: one Leaflet map hosts both the station
        // markers/popups (from the old Map tab) and the draggable bounding boxes.
        // Pre-populate job/group dropdowns + rank options for the station toggles.
        if(!wilderJobs.value.length)   loadWilderJobs();
        if(!wilderGroups.value.length) loadWilderGroups();
        loadK2RankOptions();
        loadMapAvailableTypes();
        if(!mapK2Groups.value.length) loadMapK2Groups();
        if(!exploreSchema.value) await loadExploreSchema();
        await nextTick();
        // Build the shared Leaflet map on #mbon-leaflet-map, then point the
        // bbox subsystem (_exMap) at that same instance so boxes draw on it.
        mapEnsureLeaflet();
        _exMap = _leafletMap;
        if(!_exBoxLayer) _exBoxLayer = L.layerGroup().addTo(_leafletMap);
        _leafletMap.invalidateSize();
        exRenderBboxes();
        // Station markers (layered on the same map)
        if(mapStations.value.length){
          mapRenderMarkers();
          if(mapActiveStation.value){ await nextTick(); drawMapPopupChart(); }
        } else {
          loadMapStations();
        }
        // Overview plots (driven by the bbox + filters)
        if(exResult.value){ drawExploreCharts(); } else runExplore();
      }
      if(v==='imports'){ await loadImports(); loadImportCruises(); }
      if(v==='connections'){ await loadConnections(); }
      if(v==='sql'){ if(!connList.value.length) await loadConnections(); if(!sqlTables.value.length) loadSqlTables(); if(sqlResult.value){ await nextTick(); if(sqlResultTab.value==='chart') drawSqlChart(); } }
    }

    // ── Watchers: redraw charts when tabs become visible ──────────────
    // Dashboard: v-if destroys SVG refs on nav away; redraw on return
    watch(view, async (v, old) => {
      // Tear down Leaflet when leaving the merged Explore & Map view —
      // prevents stale canvas RAF errors, and resets the shared bbox references.
      if (old === 'explore' && _leafletMap) {
        try { _leafletMap.remove(); } catch(_) {}
        _leafletMap = null; _leafletMarkers = []; _activeMarker = null;
        _exMap = null; _exBoxLayer = null;
        try { _exBoxItems.clear(); } catch(_) {}
      }
      if(v === 'dashboard'){
        await nextTick();
        if(stats.value) loadDashCharts(); else loadStats();
      }
    });
    // Wilder sub-tabs: charts silently fail if drawn while tab DOM is hidden
    watch(wilderTab, async (tab) => {
      if(tab === 'groups' && wilderGroups.value.length){
        await nextTick();
        drawGroupChart();
        drawDonut(groupDonutSvg.value,groupDonutWrap.value,groupDonutLegend.value,
          wilderGroups.value.slice(0,14),{labelKey:'organism_group',valueKey:'total_reads',colorFn:groupColor});
      } else if(tab === 'profiles' && wilderTopTaxa.value.length){
        await nextTick();
        drawWilderChart();
        drawWilderLolli();
      } else if(tab === 'sunburst'){
        if(!wilderSbSamples.value.length) await loadWilderSbSamples();
        const p0=wilderSbPanels.value[0];
        if(p0.data.length){ await nextTick(); drawWilderSbPanel(p0); }
      } else if(tab === 'results'){
        if(!wilderResults.value.length && !wilderLoading.value) loadWilderResults();
      }
    });
    // Kraken2 sub-tabs: auto-load/redraw on tab switch
    watch(k2Tab, async (tab) => {
      if(tab === 'browser'){
        if(!k2Reports.value.length) await loadK2Reports();
        if(!k2TopRankOptions.value.length) await loadK2RankOptions();
        // Auto-select first report if none chosen (watcher on k2SelectedReport fires → loadK2LCAs)
        if(!k2SelectedReport.value && k2Reports.value.length){
          k2SelectedReport.value=k2Reports.value[0].id;
          return;
        }
        if(k2SelectedReport.value && !k2RankOptions.value.length) await loadK2RankOptions(k2SelectedReport.value);
        if(k2Lcas.value.length){ await nextTick(); drawK2Chart(); return; }
        if(k2SelectedReport.value && !k2LcaLoading.value) loadK2LCAs();
      } else if(tab === 'toptaxa'){
        if(k2TopTaxa.value.length){ await nextTick(); drawK2TopChart(); }
        else loadK2TopTaxa();
      } else if(tab === 'sunburst'){
        const p0=k2SbPanels.value[0];
        if(!p0.report && k2Reports.value.length) p0.report=k2Reports.value[0].id;
        if(p0.data.length){ await nextTick(); drawSbPanel(p0); return; }
        if(p0.report && !p0.loading) loadSbPanel(p0);
      } else if(tab === 'compare'){
        if(!k2Reports.value.length) loadK2Reports();
        if(!k2TopRankOptions.value.length) loadK2RankOptions();
      }
    });
    // Kraken2 LCA: load only ranks that exist for the selected report and keep a valid selection.
    watch(k2SelectedReport, async (report) => { if(report){ await loadK2RankOptions(report); loadK2LCAs(); } });
    watch(k2RankFilter, () => { if(k2SelectedReport.value) loadK2LCAs(); });
    // Kraken2 Top Taxa: auto-reload on rank change
    watch(k2TopRank, () => loadK2TopTaxa());
    watch(k2TopRankOptions, opts => {
      const next = chooseRank(opts, k2CompareRank.value);
      if (next !== k2CompareRank.value) k2CompareRank.value = next;
    });
    // Wilderlab profiles: auto-reload when group or job filter changes
    watch([wilderTopGroup, wilderTopJob, wilderTopSample], () => { if(view.value==='wilder') loadWilderTopTaxa(); });
    // Wilderlab Results Table: auto-reload when job/group/sample dropdown filters change
    watch([wilderJobFilter, wilderGroupFilter, wilderSampleFilter], () => {
      if(view.value==='wilder' && wilderTab.value==='results') loadWilderResults();
    });

    // Name-tag auto-redraw: charts update immediately when tags change (no Load needed)
    watch(k2NameTags, () => { if(k2Lcas.value.length) drawK2Chart(); }, { deep: true });
    watch(k2TopNameTags, () => { if(k2TopTaxa.value.length) drawK2TopChart(); }, { deep: true });
    watch(wNameTags, () => { if(wilderTopTaxa.value.length){ drawWilderChart(); drawWilderLolli(); } }, { deep: true });

    // Wilder compare panels: watch computed data so chart redraws after DOM is updated
    // flush:'post' ensures the watcher runs after Vue has applied DOM changes (v-if panel visible, ref populated)
    watch(wilderResCompareData, val => { if(val) nextTick(drawWilderResCompare); }, { flush:'post' });
    watch(wilderTopCompareData, val => { if(val) nextTick(drawWilderTopCompare); }, { flush:'post' });

    // Map: redraw chart when active station or topN changes
    watch(mapActiveStation, async () => {
      if (mapActiveStation.value) { await nextTick(); drawMapPopupChart(); }
    });
    watch(mapTopN, async () => {
      if (mapActiveStation.value) { await nextTick(); drawMapPopupChart(); }
    });
    // Map name filter: re-render markers immediately when tags change
    watch(filteredMapStations, async () => {
      if (!mapLeafletReady()) return;
      // Clear active station if it was filtered out
      if (mapActiveStation.value && !filteredMapStations.value.includes(mapActiveStation.value)) {
        mapActiveStation.value = null;
      }
      mapRenderMarkers();
    }, { deep: false });
    // Map compare: redraw when selection or compare view changes
    watch(selectedStationObjects, async () => {
      if (mapCompareOpen.value && selectedStationObjects.value.length) { await nextTick(); drawMapCompare(); }
    }, { deep: false });
    watch(mapCompareOpen, async (open) => {
      if (open && selectedStationObjects.value.length) { await nextTick(); drawMapCompare(); }
    });

    // ── Helpers ───────────────────────────────────────────────────────
    function fmt(n){ return n==null?'–':Number(n).toLocaleString(); }
    function fmtCompact(n){ return fmtCompactNumber(n); }
    function fmtDate(d){ return d?new Date(d).toLocaleDateString('en-US',{year:'numeric',month:'short',day:'numeric'}):'–'; }
    function sampleTypePill(t){ return {SAMPLE:'pill-sample',POSITIVE_CONTROL:'pill-pos',NEGATIVE_CONTROL:'pill-neg'}[t]||'pill-other'; }

    /* ═══════════════════════════════════════════════════════════════════
       EXPLORE — env-range + species cross-filter
    ═══════════════════════════════════════════════════════════════════ */
    const exploreSchema=ref(null), exploreLoading=ref(false), exploreError=ref('');
    const exResult=ref(null), exQuerying=ref(false);
    const exFiltersCollapsed=ref(false);   // collapse the left filter panel to widen the map
    const exFilters=ref({ depthMin:'',depthMax:'', salMin:'',salMax:'', tempMin:'',tempMax:'',
                          sstMin:'',sstMax:'', s:'',n:'',w:'',e:'', search:'', minReads:'', top:50 });
    const exGroups=ref([]), exRanks=ref([]), exSpeciesInput=ref('');  // exSpecies declared earlier (needed by filteredMapStations)
    const exScatterX=ref('salinity'), exScatterY=ref('depth'), exEnvVar=ref('salinity');
    const exScatterSize=ref('read_count');
    const exSpeciesBarSvg=ref(null), exSpeciesBarWrap=ref(null), exSpeciesBarLegend=ref(null);
    const exScatterSvg=ref(null), exScatterWrap=ref(null);
    const exEnvSvg=ref(null), exEnvWrap=ref(null);
    const exBboxes=ref([]), exActiveBboxId=ref(null);
    let _exMap=null, _exLayer=null, _exClusterGroup=null, _exBoxLayer=null, _exNextBboxId=1, _exBboxTimer=null;
    const _exBoxItems=new Map();

    // Species-bar Top-N + pagination (so a 500+ species result stays readable).
    const exBarTopN=ref(15), exBarPage=ref(0);
    // Full ranked list (reads > 0), sorted desc — the pool the bar pages through.
    const exBarAllSpecies=computed(()=>{
      const sp=exResult.value?.species||[];
      return sp.filter(s=>+s.total_reads>0)
               .slice().sort((a,b)=>(+b.total_reads)-(+a.total_reads));
    });
    const exBarPages=computed(()=>{
      const n=Math.max(1, Number(exBarTopN.value)||15);
      return Math.max(1, Math.ceil(exBarAllSpecies.value.length/n));
    });
    const exBarSlice=computed(()=>{
      const n=Math.max(1, Number(exBarTopN.value)||15);
      const p=Math.min(exBarPage.value, exBarPages.value-1);
      return exBarAllSpecies.value.slice(p*n, p*n+n);
    });
    const exBarRangeLabel=computed(()=>{
      const total=exBarAllSpecies.value.length;
      if(!total) return '0 of 0';
      const n=Math.max(1, Number(exBarTopN.value)||15);
      const p=Math.min(exBarPage.value, exBarPages.value-1);
      return `${p*n+1}–${Math.min(p*n+n,total)} of ${total}`;
    });

    const exEnvAvailable=computed(()=> exploreSchema.value?.env_available || []);
    // every numeric field actually present in the detection points — drives the
    // scatter X / Y / size selectors so they aren't limited to env columns.
    const exScatterFields=computed(()=>{
      const pts=exResult.value?.points||[];
      const skip=new Set(['scientific_name','organism_group','common_name','taxonomic_rank']);
      const fields=new Set();
      pts.slice(0,200).forEach(p=>Object.keys(p).forEach(k=>{
        if(skip.has(k))return;
        if(p[k]!=null && !isNaN(+p[k])) fields.add(k);
      }));
      // keep env columns first for familiarity
      const env=exEnvAvailable.value.filter(e=>fields.has(e));
      const rest=[...fields].filter(f=>!env.includes(f)).sort();
      return [...env,...rest];
    });
    const exActiveFilterCount=computed(()=>{
      const f=exFilters.value; let c=0;
      ['depth','sal','temp','sst'].forEach(k=>{ if(f[k+'Min']!==''||f[k+'Max']!=='')c++; });
      if(exBboxes.value.length || f.s!==''||f.n!==''||f.w!==''||f.e!=='')c++;
      c+=exGroups.value.length?1:0; c+=exRanks.value.length?1:0; c+=exSpecies.value.length?1:0;
      if(f.search)c++; if(f.minReads)c++;
      return c;
    });
    const exActiveBbox=computed(()=> exBboxes.value.find(b=>b.id===exActiveBboxId.value) || null);

    function _numOrNull(v){ return v===''||v==null ? null : Number(v); }
    function _roundCoord(v){ return Number.isFinite(+v) ? Number(+v).toFixed(5).replace(/\.0+$/,'').replace(/(\.\d*?)0+$/,'$1') : ''; }
    function exNormalizeBbox(box){
      if(!box) return null;
      const vals={s:_numOrNull(box.s),n:_numOrNull(box.n),w:_numOrNull(box.w),e:_numOrNull(box.e)};
      if(Object.values(vals).some(v=>v==null || !Number.isFinite(v))) return null;
      const [s,n]=[vals.s,vals.n].sort((a,b)=>a-b);
      const [w,e]=[vals.w,vals.e].sort((a,b)=>a-b);
      return {s,n,w,e};
    }
    function exBboxLabel(box){ return box ? `${_roundCoord(box.s)}, ${_roundCoord(box.w)} → ${_roundCoord(box.n)}, ${_roundCoord(box.e)}` : ''; }
    function exSyncInputsFromBbox(box){
      if(!box) return;
      Object.assign(exFilters.value,{s:_roundCoord(box.s),n:_roundCoord(box.n),w:_roundCoord(box.w),e:_roundCoord(box.e)});
    }
    function exInputBbox(){ return exNormalizeBbox({s:exFilters.value.s,n:exFilters.value.n,w:exFilters.value.w,e:exFilters.value.e}); }

    async function loadExploreSchema(){
      exploreLoading.value=true; exploreError.value='';
      try{
        const r=await axios.get(`${API}/explore/schema`);
        if(r.data.error){ exploreError.value=r.data.error; exploreSchema.value=null; }
        else exploreSchema.value=r.data;
      }catch(e){ exploreError.value='Could not load schema: '+(e.response?.data?.error||e.message); }
      finally{ exploreLoading.value=false; }
    }

    function exToggle(arrRef,val){
      const target = arrRef && Array.isArray(arrRef.value) ? arrRef : (arrRef===exGroups.value ? exGroups : exRanks);
      const i=target.value.indexOf(val);
      if(i>=0) target.value=target.value.filter(x=>x!==val); else target.value=[...target.value,val];
      runExplore();
    }
    // Kraken2 group toggle — filters Kraken2 map markers client-side (no wilder requery)
    function exToggleK2Group(g){
      const i=exK2Groups.value.indexOf(g);
      if(i>=0) exK2Groups.value=exK2Groups.value.filter(x=>x!==g);
      else exK2Groups.value=[...exK2Groups.value,g];
    }
    function exAddSpecies(){
      const v=exSpeciesInput.value.trim();
      if(v&&!exSpecies.value.includes(v)){ exSpecies.value=[...exSpecies.value,v]; runExplore(); }
      exSpeciesInput.value='';
    }
    function exRemoveSpecies(v){ exSpecies.value=exSpecies.value.filter(x=>x!==v); runExplore(); }
    function exToggleSpeciesFromBar(name){
      if(exSpecies.value.includes(name)) exSpecies.value=exSpecies.value.filter(x=>x!==name);
      else exSpecies.value=[...exSpecies.value,name];
      runExplore();
    }

    function exUseFullBbox(){
      const b=exploreSchema.value?.bbox; if(!b)return;
      if(exActiveBbox.value) exUpdateBbox(exActiveBbox.value.id,b,true);
      else exAddBbox(b);
    }
    function exReset(){
      exFilters.value={ depthMin:'',depthMax:'', salMin:'',salMax:'', tempMin:'',tempMax:'',
                        sstMin:'',sstMax:'', s:'',n:'',w:'',e:'', search:'', minReads:'', top:50 };
      exGroups.value=[]; exRanks.value=[]; exSpecies.value=[]; exK2Groups.value=[]; exBboxes.value=[]; exActiveBboxId.value=null; exRenderBboxes(); runExplore();
    }

    function exSetActiveBbox(id){
      exActiveBboxId.value=id;
      const box=exActiveBbox.value;
      if(box) exSyncInputsFromBbox(box);
      exRenderBboxes();
    }
    function exDefaultBbox(){
      if(exMapReady()){
        const b=_exMap.getBounds();
        const padLat=(b.getNorth()-b.getSouth())*0.25, padLng=(b.getEast()-b.getWest())*0.25;
        return {s:b.getSouth()+padLat,n:b.getNorth()-padLat,w:b.getWest()+padLng,e:b.getEast()-padLng};
      }
      return exploreSchema.value?.bbox || exInputBbox();
    }
    function exAddBbox(box=null){
      const normalized=exNormalizeBbox(box || exDefaultBbox());
      if(!normalized) return;
      const item={id:_exNextBboxId++,...normalized};
      exBboxes.value=[...exBboxes.value,item];
      exActiveBboxId.value=item.id;
      exSyncInputsFromBbox(item);
      exRenderBboxes();
      exScheduleExplore();
    }
    function exRefreshBboxGraphics(id,skipCorner=null){
      const item=_exBoxItems.get(id), box=exBboxes.value.find(b=>b.id===id);
      if(!item || !box) return;
      item.rect.setBounds(exBboxBounds(box));
      item.rect.setTooltipContent?.(`Box ${exBboxes.value.findIndex(b=>b.id===id)+1}<br>${exBboxTooltip(box)}`);
      Object.entries(item.handles||{}).forEach(([corner,marker])=>{
        if(corner!==skipCorner) marker.setLatLng(exCornerLatLng(box,corner));
        marker.setTooltipContent?.(exCornerTooltip(box,corner));
      });
    }
    function exUpdateBbox(id,box,run=false,render=true){
      const normalized=exNormalizeBbox(box); if(!normalized) return;
      exBboxes.value=exBboxes.value.map(b=>b.id===id?{...b,...normalized}:b);
      if(exActiveBboxId.value===id) exSyncInputsFromBbox({...normalized,id});
      if(render) exRenderBboxes(); else exRefreshBboxGraphics(id);
      if(run) exScheduleExplore();
    }
    function exSyncActiveBboxFromInputs(){
      const box=exInputBbox(); if(!box) return;
      if(exActiveBbox.value) exUpdateBbox(exActiveBbox.value.id,box,true);
      else exAddBbox(box);
    }
    function exDeleteActiveBbox(){
      const id=exActiveBboxId.value; if(id==null) return;
      exBboxes.value=exBboxes.value.filter(b=>b.id!==id);
      exActiveBboxId.value=exBboxes.value[0]?.id ?? null;
      if(exActiveBbox.value) exSyncInputsFromBbox(exActiveBbox.value);
      else Object.assign(exFilters.value,{s:'',n:'',w:'',e:''});
      exRenderBboxes(); exScheduleExplore();
    }
    function exClearBboxes(){
      exBboxes.value=[]; exActiveBboxId.value=null; Object.assign(exFilters.value,{s:'',n:'',w:'',e:''});
      exRenderBboxes(); exScheduleExplore();
    }
    function exScheduleExplore(){ clearTimeout(_exBboxTimer); _exBboxTimer=setTimeout(runExplore,250); }

    async function runExplore(){
      exQuerying.value=true; exploreError.value='';
      const f=exFilters.value;
      const body={ top:Number(f.top)||50 };
      const rng=(mn,mx,key)=>{ const a=_numOrNull(f[mn]),b=_numOrNull(f[mx]); if(a!=null||b!=null) body[key]=[a,b]; };
      rng('depthMin','depthMax','depth'); rng('salMin','salMax','salinity');
      rng('tempMin','tempMax','temp'); rng('sstMin','sstMax','sst');
      const boxes=exBboxes.value.map(exNormalizeBbox).filter(Boolean);
      const inputBox=boxes.length ? null : exInputBbox();
      if(boxes.length) body.bboxes=boxes;
      else if(inputBox) body.bbox=inputBox;
      if(exGroups.value.length) body.groups=exGroups.value;
      if(exRanks.value.length)  body.ranks=exRanks.value;
      if(exSpecies.value.length) body.species=exSpecies.value;
      if(f.search) body.search=f.search;
      if(f.minReads) body.min_reads=Number(f.minReads);
      try{
        const r=await axios.post(`${API}/explore/query`, body);
        if(r.data.error){ exploreError.value=r.data.error; exResult.value=null; }
        else { exResult.value=r.data; exBarPage.value=0; await nextTick(); drawExploreCharts(); exRenderBboxes(); }
      }catch(e){ exploreError.value='Query failed: '+(e.response?.data?.error||e.message); }
      finally{ exQuerying.value=false; }
    }

    // Draw only the species bar (used by the Top-N / pager watchers without
    // redrawing the map, scatter and envelope every time).
    function drawExploreSpeciesBar(){
      if(!exResult.value) return;
      const slice=exBarSlice.value;
      drawBar('exSpeciesBar', exSpeciesBarSvg.value, exSpeciesBarWrap.value, slice, {
        labelKey:'scientific_name', valueKey:'total_reads', italic:true,
        colorFn:d=>groupColor(d.organism_group),
        fmtVal:fmtCompact,
        highlightSet:new Set(exSpecies.value),
        onClickBar:d=>exToggleSpeciesFromBar(d.scientific_name),
        fmtTip:d=>`<strong>${d.scientific_name}</strong>${d.common_name?` (${d.common_name})`:''}<br>${d.organism_group||''} · ${d.taxonomic_rank||''}<br>${(+d.total_reads).toLocaleString()} reads · ${d.sample_count} samples`,
      });
      // Pinned legend: organism groups present in the full result (stable while paging).
      const leg=exSpeciesBarLegend.value;
      if(leg){
        const escG=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        const groups=[...new Set(exBarAllSpecies.value.map(s=>s.organism_group||'—'))];
        leg.innerHTML=groups.map(g=>
          `<span class="legend-item"><span class="legend-swatch" style="background:${groupColor(g==='—'?'':g)}"></span>${escG(g)}</span>`
        ).join('');
      }
    }

    function drawExploreCharts(){
      const res=exResult.value; if(!res) return;
      resetBarZoom('exSpeciesBar');
      drawExploreSpeciesBar();
      drawScatter(exScatterSvg.value, exScatterWrap.value, res.points, {
        xKey:exScatterX.value, yKey:exScatterY.value, sizeKey:exScatterSize.value||'read_count', colorKey:'organism_group',
        invertY:exScatterY.value==='depth',
      });
      drawEnvelope(exEnvSvg.value, exEnvWrap.value, res.envelopes, { envKey:exEnvVar.value });
    }

    function exMapReady(){ return _exMap && typeof L!=='undefined'; }
    // In the merged Explore & Map view the bbox subsystem shares the main
    // Leaflet map (_leafletMap). This just binds to it and ensures a box layer.
    function exEnsureMap(){
      if(typeof L==='undefined') return;
      if(!_exMap && _leafletMap) _exMap=_leafletMap;
      if(!_exMap) return;
      if(!_exBoxLayer) _exBoxLayer=L.layerGroup().addTo(_exMap);
      _exMap.invalidateSize();
    }
    function exClearMapLayers(){
      if(_exClusterGroup){ try{ _exClusterGroup.clearLayers(); _exMap.removeLayer(_exClusterGroup); }catch(_){} _exClusterGroup=null; }
      if(_exLayer){ try{ _exLayer.clearLayers(); _exMap.removeLayer(_exLayer); }catch(_){} _exLayer=null; }
    }
    function exBboxBounds(box){ return [[box.s,box.w],[box.n,box.e]]; }
    function exBboxTooltip(box){
      return `S ${_roundCoord(box.s)}, N ${_roundCoord(box.n)}<br>W ${_roundCoord(box.w)}, E ${_roundCoord(box.e)}`;
    }
    function exCornerLatLng(box,corner){
      return {sw:[box.s,box.w],se:[box.s,box.e],nw:[box.n,box.w],ne:[box.n,box.e]}[corner];
    }
    function exCornerTooltip(box,corner){
      const ll=exCornerLatLng(box,corner);
      return `${corner.toUpperCase()} ${_roundCoord(ll[0])}, ${_roundCoord(ll[1])}`;
    }
    function exCornerIcon(active){
      return L.divIcon({className:`ex-bbox-handle ${active?'active':''}`,html:'',iconSize:[12,12],iconAnchor:[6,6]});
    }
    function exStartBboxDrag(id,start){
      const original=exBboxes.value.find(b=>b.id===id); if(!original || !_exMap) return;
      const startLatLng=start.latlng;
      _exMap.dragging.disable();
      const move=e=>{
        const dLat=e.latlng.lat-startLatLng.lat, dLng=e.latlng.lng-startLatLng.lng;
        exUpdateBbox(id,{s:original.s+dLat,n:original.n+dLat,w:original.w+dLng,e:original.e+dLng},false,false);
      };
      const up=()=>{
        _exMap.off('mousemove',move); _exMap.off('mouseup',up); _exMap.dragging.enable(); exScheduleExplore();
      };
      _exMap.on('mousemove',move); _exMap.on('mouseup',up);
    }
    function exRenderBboxes(){
      if(!exMapReady()) return;
      if(!_exBoxLayer) _exBoxLayer=L.layerGroup().addTo(_exMap);
      _exBoxLayer.clearLayers(); _exBoxItems.clear();
      exBboxes.value.forEach((box,index)=>{
        const active=box.id===exActiveBboxId.value;
        const color=active?'#e07b39':'#0077b6';
        const rect=L.rectangle(exBboxBounds(box),{color,weight:active?3:2,fillColor:color,fillOpacity:0.08,dashArray:active?null:'5 4'}).addTo(_exBoxLayer);
        rect.bindTooltip(`Box ${index+1}<br>${exBboxTooltip(box)}`,{sticky:true,className:'ex-bbox-tooltip'});
        rect.on('click',()=>exSetActiveBbox(box.id));
        rect.on('mousedown',e=>{ exSetActiveBbox(box.id); exStartBboxDrag(box.id,e); });
        const handles={};
        ['sw','se','nw','ne'].forEach(corner=>{
          const marker=L.marker(exCornerLatLng(box,corner),{draggable:true,icon:exCornerIcon(active),zIndexOffset:active?900:800}).addTo(_exBoxLayer);
          marker.bindTooltip(exCornerTooltip(box,corner),{sticky:true,className:'ex-bbox-tooltip'});
          marker.on('click',()=>exSetActiveBbox(box.id));
          marker.on('drag',e=>{
            const current=exBboxes.value.find(b=>b.id===box.id); if(!current) return;
            const next={...current};
            const lat=e.target.getLatLng().lat, lng=e.target.getLatLng().lng;
            if(corner.includes('s')) next.s=lat; else next.n=lat;
            if(corner.includes('w')) next.w=lng; else next.e=lng;
            exUpdateBbox(box.id,next,false,false);
            exRefreshBboxGraphics(box.id,corner);
          });
          marker.on('dragend',()=>exScheduleExplore());
          handles[corner]=marker;
        });
        _exBoxItems.set(box.id,{rect,handles});
      });
    }
    function exClusterIcon(cluster){
      const n=cluster.getChildCount();
      const sizeClass=n<5?'sm':(n<15?'md':'lg');
      const dim=n<5?34:(n<15?40:48);
      const children=cluster.getAllChildMarkers?cluster.getAllChildMarkers():[];
      const totalReads=children.reduce((sum,m)=>sum+(+m._exSample?.total_reads||0),0);
      const readBg=totalReads>1000000?'#023e8a':(totalReads>100000?'#0077b6':'#2ec4b6');
      const title=`${n} sample(s), ${fmt(totalReads)} reads`;
      return L.divIcon({
        html:`<div class="mbon-cluster mbon-cluster-${sizeClass}" title="${mapEsc(title)}" style="--sample-bg:${readBg};--run-bg:rgba(2,62,138,.78);--project-bg:rgba(15,118,110,.9)"><i class="mbon-cluster-run-ring"></i><i class="mbon-cluster-project-ring"></i><span>${n}</span></div>`,
        className:'mbon-cluster-wrap',
        iconSize:L.point(dim,dim),
      });
    }
    function exClusterPopupHtml(cluster){
      const markers=(cluster.getAllChildMarkers?cluster.getAllChildMarkers():[])
        .slice().sort((a,b)=>(+b._exSample?.total_reads||0)-(+a._exSample?.total_reads||0));
      const totalReads=markers.reduce((sum,m)=>sum+(+m._exSample?.total_reads||0),0);
      const speciesTotal=markers.reduce((sum,m)=>sum+(+m._exSample?.species_count||0),0);
      const rows=markers.slice(0,40).map(m=>{
        const s=m._exSample||{};
        const label=s.label||s.sample_key||'Sample';
        const env=[s.station, s.depth!=null?`depth ${s.depth} m`:null, s.salinity!=null?`sal ${s.salinity}`:null, s.temp!=null?`temp ${s.temp}°`:null]
          .filter(Boolean).join(' · ');
        return `<div class="mbon-cluster-row"><span class="mbon-cluster-swatch" style="background:#0077b6"></span><strong>${mapEsc(label)}</strong><small>${mapEsc(env || 'Explore sample')} · ${fmtCompact(s.total_reads)} reads · ${mapEsc(s.species_count||0)} species</small></div>`;
      }).join('');
      const more=markers.length>40?`<div class="mbon-cluster-more">+ ${markers.length-40} more sample(s)</div>`:'';
      return `<div class="mbon-cluster-popup"><div class="mbon-cluster-popup-head"><strong>${markers.length} sample(s)</strong><span>${fmtCompact(totalReads)} reads · ${fmtCompact(speciesTotal)} species detections</span></div>${rows}${more}</div>`;
    }
    function exRenderMap(){
      if(!exMapReady()){ exEnsureMap(); if(!exMapReady())return; }
      exClearMapLayers();
      exRenderBboxes();
      const samples=(exResult.value?.samples||[]).filter(s=>s.latitude!=null&&s.longitude!=null);
      if(!samples.length) return;
      const rMax=d3.max(samples,s=>+s.total_reads||0)||1;
      const rs=d3.scaleSqrt().domain([0,rMax]).range([4,18]);
      const useCluster=typeof L.markerClusterGroup==='function';
      if(useCluster){
        _exClusterGroup=L.markerClusterGroup({
          showCoverageOnHover:false,
          maxClusterRadius:60,
          spiderfyDistanceMultiplier:1.35,
          spiderfyOnMaxZoom:true,
          zoomToBoundsOnClick:true,
          iconCreateFunction:exClusterIcon,
        }).addTo(_exMap);
        _exClusterGroup.on('clustermouseover',e=>{
          try{ e.layer.bindTooltip(`${e.layer.getChildCount()} sample(s)`,{sticky:true}).openTooltip(); }catch(_){}
        });
        _exClusterGroup.on('clusterclick',e=>{
          try{ e.layer.bindPopup(exClusterPopupHtml(e.layer),{maxWidth:360}).openPopup(); }catch(_){}
        });
      }else{
        _exLayer=L.layerGroup().addTo(_exMap);
      }
      const addToMap=m=>{ if(useCluster) _exClusterGroup.addLayer(m); else m.addTo(_exLayer); };
      const pts=[];
      samples.forEach(s=>{
        const m=L.circleMarker([s.latitude,s.longitude],{radius:rs(+s.total_reads||0),
          color:'#023e8a',weight:1,fillColor:'#0077b6',fillOpacity:.6});
        m._exSample=s;
        m.bindPopup(`<strong>${mapEsc(s.label||s.sample_key)}</strong>${s.station?`<br>${mapEsc(s.station)}`:''}<br>${mapEsc(s.species_count)} species · ${fmt(s.total_reads)} reads<br>${s.depth!=null?`depth ${mapEsc(s.depth)} m · `:''}${s.salinity!=null?`sal ${mapEsc(s.salinity)} · `:''}${s.temp!=null?`temp ${mapEsc(s.temp)}°`:''}`);
        addToMap(m); pts.push([s.latitude,s.longitude]);
      });
      if(pts.length && !exBboxes.value.length) _exMap.fitBounds(pts,{padding:[30,30],maxZoom:9});
    }
    function exTeardownMap(){ if(_exMap){ try{exClearMapLayers(); _exBoxLayer?.clearLayers(); _exMap.remove();}catch(_){} _exMap=null; _exLayer=null; _exClusterGroup=null; _exBoxLayer=null; _exBoxItems.clear(); } }

    watch([exScatterX,exScatterY,exScatterSize],()=>{ if(exResult.value) drawExploreCharts(); });
    watch(exEnvVar,()=>{ if(exResult.value) drawExploreCharts(); });
    // Top-N change: clamp page and redraw just the bar (keep zoom reset so the
    // new set starts at the top). Page change: redraw bar in place.
    watch(exBarTopN,()=>{ if(!exResult.value)return; exBarPage.value=0; resetBarZoom('exSpeciesBar'); nextTick(drawExploreSpeciesBar); });
    watch(exBarPage,()=>{ if(!exResult.value)return; resetBarZoom('exSpeciesBar'); nextTick(drawExploreSpeciesBar); });
    watch(view,(v,old)=>{ if(old==='explore') exTeardownMap(); });
    // Collapsing/expanding the filter panel changes the map width → re-measure Leaflet.
    watch(exFiltersCollapsed,()=>{
      if(view.value==='explore' && _leafletMap){
        nextTick(()=>{ try{ _leafletMap.invalidateSize(); }catch(_){} });
      }
    });

    /* ═══════════════════════════════════════════════════════════════════
       CUSTOM SQL — read-only SELECT runner + saved queries (localStorage)
    ═══════════════════════════════════════════════════════════════════ */
    const SQL_LS='mbon.sql.saved';
    const sqlText=ref(''), sqlLimit=ref(1000);
    const sqlRunning=ref(false), sqlError=ref(''), sqlResult=ref(null);
    const sqlResultTab=ref('table');
    const sqlSaved=ref([]);
    const sqlTables=ref([]), sqlTableFilter=ref('');
    const sqlChartSvg=ref(null), sqlChartWrap=ref(null);
    const sqlChartType=ref('bar'), sqlChartX=ref(''), sqlChartY=ref(''), sqlChartSize=ref('');
    const sqlChartTopN=ref(40);

    function sqlLoadSaved(){ try{ sqlSaved.value=JSON.parse(localStorage.getItem(SQL_LS)||'[]'); }catch(e){ sqlSaved.value=[]; } }
    function sqlPersist(){ try{ localStorage.setItem(SQL_LS,JSON.stringify(sqlSaved.value)); }catch(e){} }
    sqlLoadSaved();

    const sortedSqlRows=computed(()=>{
      const s=tableSorts.value['sqlres'];
      return applySort(s,(sqlResult.value?.rows)||[]);
    });
    const sqlNumericColumns=computed(()=>{
      const r=sqlResult.value; if(!r||!r.rows.length) return r?r.columns:[];
      return r.columns.filter(c=> r.rows.some(row=>row[c]!=null) && r.rows.every(row=> row[c]==null || !isNaN(+row[c])));
    });

    async function runSql(){
      if(!sqlText.value.trim()){ sqlError.value='Enter a query.'; return; }
      sqlRunning.value=true; sqlError.value='';
      try{
        const r=await axios.post(`${API}/sql/query`,{ sql:sqlText.value, limit:sqlLimit.value });
        if(r.data.error){ sqlError.value=r.data.error; sqlResult.value=null; }
        else{
          sqlResult.value=r.data;
          tableSorts.value={...tableSorts.value, sqlres:{key:null,dir:'desc'}};
          // sensible default chart axes
          if(r.data.columns.length){
            sqlChartX.value=r.data.columns[0];
            const nums=r.data.columns.filter(c=> r.data.rows.some(row=>row[c]!=null) && r.data.rows.every(row=> row[c]==null || !isNaN(+row[c])));
            sqlChartY.value=nums[0]||r.data.columns[1]||r.data.columns[0];
          }
          if(sqlResultTab.value==='chart'){ await nextTick(); drawSqlChart(); }
        }
      }catch(e){ sqlError.value=e.response?.data?.error || e.message || 'Query failed'; sqlResult.value=null; }
      finally{ sqlRunning.value=false; }
    }
    function onSqlKey(e){ if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){ e.preventDefault(); runSql(); } }
    function sqlSaveCurrent(){
      if(!sqlText.value.trim())return;
      const name=prompt('Name this query:', (sqlText.value.trim().slice(0,40)));
      if(!name)return;
      sqlSaved.value=[{id:Date.now(),name,sql:sqlText.value},...sqlSaved.value];
      sqlPersist();
    }
    function sqlLoad(q){ sqlText.value=q.sql; }
    function sqlDelete(id){ sqlSaved.value=sqlSaved.value.filter(q=>q.id!==id); sqlPersist(); }
    function sqlInsert(token){
      sqlText.value=(sqlText.value && !/\s$/.test(sqlText.value)? sqlText.value+' ':sqlText.value)+token+' ';
    }
    async function loadSqlTables(){
      try{ const r=await axios.get(`${API}/sql/tables`); sqlTables.value=(r.data.tables||[]).map(t=>({...t,_open:false})); }
      catch(e){ sqlTables.value=[]; }
    }
    const filteredSqlTables=computed(()=>{
      const f=sqlTableFilter.value.toLowerCase().trim();
      if(!f) return sqlTables.value;
      return sqlTables.value.filter(t=> t.name.toLowerCase().includes(f) || t.columns.some(c=>c.name.toLowerCase().includes(f)));
    });
    function drawSqlChart(){
      const r=sqlResult.value; if(!r||!sqlChartSvg.value) return;
      const x=sqlChartX.value, y=sqlChartY.value;
      if(!x||!y) return;
      const rows=sortedSqlRows.value;
      if(sqlChartType.value==='scatter'){
        const sz=sqlChartSize.value;
        const pts=rows.map(row=>({ [x]:+row[x], [y]:+row[y], [sz||'_s']:sz?+row[sz]:1, scientific_name:String(row[x]) }))
                      .filter(p=>!isNaN(p[x])&&!isNaN(p[y]));
        drawScatter(sqlChartSvg.value, sqlChartWrap.value, pts, { xKey:x, yKey:y, sizeKey:sz||'_s', colorKey:'__none', invertY:false });
      } else {
        const data=rows.slice(0,sqlChartTopN.value).map(row=>({ name:String(row[x]), value:+row[y] })).filter(d=>!isNaN(d.value));
        drawHBar(sqlChartSvg.value, sqlChartWrap.value, data, {
          labelKey:'name', valueKey:'value', colorFn:()=>'#0077b6',
          fmtTip:d=>`<strong>${d.name}</strong><br>${y}: ${d.value.toLocaleString()}`,
        });
      }
    }
    watch([sqlChartType,sqlChartX,sqlChartY,sqlChartSize],()=>{ if(sqlResult.value&&sqlResultTab.value==='chart') nextTick(drawSqlChart); });

    /* ═══════════════════════════════════════════════════════════════════
       CONNECTIONS — data-source registry
    ═══════════════════════════════════════════════════════════════════ */
    const connList=ref([]), connLoading=ref(false), connMsg=ref('');
    const connTests=ref({});            // id -> test result
    const connForm=ref(null);           // null = no form open
    const connSaving=ref(false);

    const activeConn=computed(()=> connList.value.find(c=>c.active) || null);

    async function loadConnections(){
      connLoading.value=true;
      try{ connList.value=(await axios.get(`${API}/connections`)).data; }
      catch(e){ connMsg.value='Could not load connections: '+e.message; }
      finally{ connLoading.value=false; }
    }
    function connNew(kind){
      const base={ name:'', kind, readonly:true, config:{} };
      if(kind==='postgres') base.config={ host:'127.0.0.1',port:'5432',dbname:'',user:'',password:'' };
      if(kind==='xlsx') base.config={ path:'', samples_sheet:'', obs_sheet:'' };
      if(kind==='gsheet') base.config={ url:'', samples_sheet:'', obs_sheet:'' };
      connForm.value=base;
    }
    function connEdit(c){ connForm.value=JSON.parse(JSON.stringify(c)); }
    function connCancel(){ connForm.value=null; }
    async function connSave(){
      const f=connForm.value; if(!f) return;
      connSaving.value=true; connMsg.value='';
      try{
        if(f.id) await axios.put(`${API}/connections/${f.id}`, f);
        else     await axios.post(`${API}/connections`, f);
        connForm.value=null; await loadConnections();
      }catch(e){ connMsg.value='Save failed: '+(e.response?.data?.error||e.message); }
      finally{ connSaving.value=false; }
    }
    async function connDelete(id){
      if(!confirm('Delete this data source?')) return;
      try{ await axios.delete(`${API}/connections/${id}`); await loadConnections(); }
      catch(e){ connMsg.value='Delete failed: '+e.message; }
    }
    async function connTest(id){
      connTests.value={...connTests.value,[id]:{loading:true}};
      try{ const r=await axios.post(`${API}/connections/${id}/test`);
        connTests.value={...connTests.value,[id]:r.data}; }
      catch(e){ connTests.value={...connTests.value,[id]:{ok:false,error:e.message}}; }
    }
    async function connActivate(id){
      try{
        await axios.post(`${API}/connections/${id}/activate`);
        await loadConnections();
        exploreSchema.value=null; exResult.value=null;
        loadStats();   // refresh dashboard counts against the new source
      }catch(e){ connMsg.value='Activate failed: '+e.message; }
    }
    async function connReload(id){
      connTests.value={...connTests.value,[id]:{loading:true}};
      try{ const r=await axios.post(`${API}/connections/${id}/reload`);
        connTests.value={...connTests.value,[id]:r.data}; }
      catch(e){ connTests.value={...connTests.value,[id]:{ok:false,error:e.message}}; }
    }

    // ── Browser data imports (Import side-panel + /api/import store) ─────
    const importsData    = ref(null);     // { sources, merged, counts } from API
    const importsLoading = ref(false);
    const importCruises  = ref([]);       // cruise numbers for the associate dropdown
    const importRole     = ref('wilder'); // which role table the Imports view shows
    const importSearch   = ref('');
    let importPanel = null;

    function importPanelToggle(){ if(importPanel) importPanel.toggle(true); }
    async function loadImports(){
      importsLoading.value = true;
      try { importsData.value = (await axios.get(`${API}/import`)).data; }
      catch(e){ importsData.value = { sources:[], merged:{}, counts:{} }; }
      finally { importsLoading.value = false; }
    }
    async function loadImportCruises(){
      try {
        const r = await axios.get(`${API}/mbon/cruises`);
        const set = new Set();
        (r.data||[]).forEach(c => { const v = c.cruise_number || c.Cruise_Number || c['Cruise Number'] || c.cruise; if(v) set.add(v); });
        importCruises.value = [...set].sort();
      } catch(e){ importCruises.value = []; }
    }
    const importRoleRows = computed(() => {
      const d = importsData.value; if(!d || !d.merged) return [];
      let rows = d.merged[importRole.value] || [];
      if(importSearch.value){
        let rx; try { rx = new RegExp(importSearch.value, 'i'); } catch(_){ rx = null; }
        if(rx) rows = rows.filter(r => Object.values(r).some(v => v!=null && rx.test(String(v))));
      }
      return rows;
    });
    const importRoleCols = computed(() => {
      const rows = importRoleRows.value; if(!rows.length) return [];
      return Object.keys(rows[0]).filter(k => k[0] !== '_').slice(0, 12);
    });
    const importRolesWithData = computed(() => {
      const c = (importsData.value && importsData.value.counts) || {};
      return Object.keys(c).filter(k => c[k] > 0);
    });

    function initImportPanel(){
      if(importPanel || !window.MBONImportPanel) return;
      loadImportCruises();
      importPanel = window.MBONImportPanel.create({
        getCruises: () => importCruises.value,
        onChange: async (payload) => {
          // Persist all current sources to the API, then refresh the Imports view.
          const sources = (payload.sources||[]).map(s => ({
            id:s.id, name:s.name, fileName:s.fileName, kind:s.kind, enabled:s.enabled,
            datasets:(s.datasets||[]).map(d => ({ role:d.role, sheetName:d.sheetName, headers:d.headers, records:d.records })),
            csvMeta:s.csvMeta ? { link:s.csvMeta.link } : undefined,
          }));
          try { await axios.post(`${API}/import`, { sources }); } catch(e){ /* offline: panel still works locally */ }
          await loadImports();
        },
      });
    }

    onMounted(()=>{ _tip=tooltip.value; loadStats(); initImportPanel(); });

    return {
      // explore
      exploreSchema,exploreLoading,exploreError,exResult,exQuerying,exFilters,exFiltersCollapsed,
      exGroups,exRanks,exSpecies,exSpeciesInput,exK2Groups,exScatterX,exScatterY,exEnvVar,exScatterSize,
      exBboxes,exActiveBboxId,exActiveBbox,exBboxLabel,
      exEnvAvailable,exScatterFields,exActiveFilterCount,
      exSpeciesBarSvg,exSpeciesBarWrap,exSpeciesBarLegend,exScatterSvg,exScatterWrap,exEnvSvg,exEnvWrap,
      exBarTopN,exBarPage,exBarPages,exBarRangeLabel,
      loadExploreSchema,runExplore,exReset,exToggle,exToggleK2Group,exAddSpecies,exRemoveSpecies,drawExploreSpeciesBar,
      exToggleSpeciesFromBar,exUseFullBbox,exAddBbox,exSetActiveBbox,exDeleteActiveBbox,exClearBboxes,exSyncActiveBboxFromInputs,
      // connections
      connList,connLoading,connMsg,connTests,connForm,connSaving,activeConn,
      loadConnections,connNew,connEdit,connCancel,connSave,connDelete,connTest,
      connActivate,connReload,
      // imports
      importsData,importsLoading,importCruises,importRole,importSearch,
      loadImports,importRoleRows,importRoleCols,importRolesWithData,importPanelToggle,
      tooltip,view,dbError,nav,
      stats,statsLoading,dashDonutSvg,dashDonutWrap,dashDonutLegend,dashK2Svg,dashK2Wrap,
      dashDonutAllData,dashDonutSelected,dashDonutDrillData,dashDonutDrillLoading,
      dashDonutSearch,dashDonutLegendPage,
      dashK2AllData,dashK2Search,dashK2DrillName,dashK2DrillData,dashK2DrillLoading,
      dashDonutClick,dashDonutNavGroup,redrawDashDonut,drawDashK2Chart,dashK2Click,dashK2NavToK2,
      collections,collectionsLoading,collectionSearch,collectionProjectFilter,selectedCollection,filteredCollections,
      loadCollections,
      runs,runsLoading,runSearch,runCollectionFilter,selectedRun,filteredRuns,
      projects,loadRuns,
      samples,samplesLoading,sampleSearch,sampleTypeFilter,sampleCollectionFilter,
      selectedSample,selectedSampleSummary,sortedSamples,loadSamples,openSampleDetail,
      k2Tab,k2Reports,k2Loading,k2Lcas,k2LcaLoading,k2SelectedReport,
      MAIN_RANKS,WILDER_STD_RANKS,
      k2RankFilter,k2RankOptions,k2RankSelectOptions,k2TopRankOptions,k2MinAbundance,k2Sort,k2TopN,k2ChartSvg,k2ChartWrap,k2Legend,
      k2AllowedRanks,k2ChartSearch,k2LoadedRanks,toggleK2AllowedRank,k2ShowStdRanks,k2ShowAllRanks,
      k2TopTaxa,k2TopLoading,k2TopRank,k2TopSort,k2TopSvg,k2TopWrap,k2TopLegend,
      k2HiddenRanks,k2TopHiddenRanks,
      k2NameFilter,k2NameTags,k2TopNameFilter,k2TopNameTags,
      k2ReportSearch,
      k2CompareSelected,k2CompareRank,k2CompareTop,k2CompareData,k2CompareLoading,
      k2CompareSvg,k2CompareWrap,k2CompareLegend,
      k2ComparePlotFilter,k2CompareTableSearch,k2CompareTableShowAll,
      k2ReportPickerSearch,filteredK2ReportsForPicker,filteredK2ReportsForPanel,
      k2SbPanels,sbSvgEls,sbWrapEls,sbLegendEls,sbListEls,
      addSbPanel,removeSbPanel,resetSbPanel,loadSbPanel,drawSbPanel,applySbFilters,
      k2LcaHighlighted,k2CrossName,k2CrossData,k2CrossLoading,k2CrossSvg,k2CrossWrap,
      sortedK2LCAs,sortedK2TopTaxa,filteredK2ReportsForCompare,filteredWilderCompareSamples,
      wilderCompareCruises,wilderCompareStationList,wilderSelectAll,toggleWilderByCruise,toggleWilderByStation,
      loadK2Reports,selectK2Report,loadK2LCAs,drawK2Chart,
      loadK2TopTaxa,drawK2TopChart,loadK2SunburstData,drawK2Sunburst,applyK2SunburstFilters,sbFocusOn,
      loadK2Compare,drawK2CompareChart,
      toggleK2LcaHighlight,clearK2LcaHighlights,loadK2CrossCompare,closeK2Cross,drawK2CrossChart,
      toggleK2HiddenRank,toggleK2TopHiddenRank,
      onK2NameKey,removeK2Tag,clearK2Tags,
      onK2TopNameKey,removeK2TopTag,clearK2TopTags,
      mbonSamples,mbonLoading,mbonSearch,mbonCruiseFilter,mbonStationFilter,
      mbonCruises,mbonStations,selectedMbon,sortedMbon,loadMbon,
      wilderTab,wilderResults,wilderLoading,wilderJobs,wilderJobsLoading,
      wilderGroups,wilderGroupsLoading,wilderTopTaxa,wilderTopLoading,
      wilderJobFilter,wilderGroupFilter,wilderMinReads,wilderTopGroup,wilderTopJob,wilderTopSample,
      wilderSampleFilter,wilderResSearch,
      wSort,wTopN,groupSort,wNameFilter,wNameTags,
      wAllowedRanks,wChartSearch,wLoadedRanks,toggleWAllowedRank,wShowStdRanks,wShowAllRanks,
      wilderHiddenGroups,groupBarHiddenGroups,
      wilderChartSvg,wilderChartWrap,wilderLegend,
      wilderLolliSvg,wilderLolliWrap,
      groupBarSvg,groupBarWrap,groupBarLegend,groupDonutSvg,groupDonutWrap,groupDonutLegend,
      wilderSbSamples,wilderSbPanels,wSbSvgEls,wSbWrapEls,wSbLegendEls,wSbListEls,
      wilderProfileSampleSearch,filteredWilderSbSamplesForProfile,
      wilderResultsSampleSearch,filteredWilderSbSamplesForResults,
      filteredWilderSbSamplesForPanel,
      addWilderSbPanel,removeWilderSbPanel,resetWilderSbPanel,
      applyWilderSbFilters,loadWilderSbPanel,drawWilderSbPanel,loadWilderSbSamples,
      sortedWilderResults,sortedWilderTopTaxa,sortedWilderJobs,sortedWilderGroups,
      loadWilderResults,loadWilderJobs,loadWilderGroups,loadWilderTopTaxa,
      drawWilderChart,drawGroupChart,
      toggleWilderHiddenGroup,toggleGroupBarHiddenGroup,
      wilderCompareSamples,wilderCompareSelected,wilderCompareGroup,wilderCompareTop,
      wilderCompareData,wilderCompareLoading,wilderCompareSvg,wilderCompareWrap,wilderCompareLegend,
      wilderComparePlotFilter,wilderCompareTableSearch,wilderCompareTableShowAll,
      wilderGroupsJobFilter,wilderResHighlighted,wilderResCompareSvg,wilderResCompareWrap,
      wilderResCompareData,drawWilderResCompare,toggleWilderResHighlight,clearWilderResHighlights,
      wilderTopHighlighted,wilderTopCompareRows,wilderTopCompareLoading,
      wilderTopCompareSvg,wilderTopCompareWrap,wilderTopCompareData,
      drawWilderTopCompare,toggleWilderTopHighlight,clearWilderTopHighlights,loadWilderTopCompare,
      loadWilderCompareSamples,loadWilderCompare,drawWilderCompareChart,
      onWNameKey,removeWTag,clearWTags,
      sortTable,sortArrow,
      // custom SQL
      sqlText,sqlLimit,sqlRunning,sqlError,sqlResult,sqlResultTab,
      sqlSaved,sqlTables,sqlTableFilter,filteredSqlTables,sortedSqlRows,sqlNumericColumns,
      sqlChartSvg,sqlChartWrap,sqlChartType,sqlChartX,sqlChartY,sqlChartSize,sqlChartTopN,
      runSql,onSqlKey,sqlSaveCurrent,sqlLoad,sqlDelete,sqlInsert,loadSqlTables,drawSqlChart,
      fmt,fmtCompact,fmtDate,sampleTypePill,rankColor,rankLabel,groupColor,stnCompareColor,
      // bar chart orientations
      barOrient,toggleBarOrient,resetBarZoom,
      // map
      mapAvailableTypes,mapActiveTypes,toggleMapType,mapK2RankOptions,
      mapStations,mapLoading,mapActiveStation,mapSelectedStations,mapClearSelection,mapBgClick,
      mapPopupStyle,mapPopupDragStart,mapPopupResizeStart,mapSamplesOpen,
      mapWilderJob,mapWilderGroup,mapK2Rank,mapK2Group,mapK2Groups,mapTopN,
      mapPopupChartSvg,mapPopupChartWrap,
      filteredMapStations,filteredSortedMapSpecies,filteredSortedMapTaxa,mapPopupGroupedData,
      mapStnSearch,mapStnHighlighted,
      mapWilderNameFilter,mapWilderNameTags,mapWilderSuggestions,
      mapK2NameFilter,mapK2NameTags,mapK2Suggestions,
      loadMapAvailableTypes,loadMapStations,loadMapK2Groups,drawMapPopupChart,mapClearActive,
      onMapWilderNameKey,removeMapWilderTag,clearMapWilderTags,refreshMapWilderSuggestions,
      onMapK2NameKey,removeMapK2Tag,clearMapK2Tags,refreshMapK2Suggestions,
      mapFilterBySample,mapOpenInK2,mapOpenInWilder,
      mapPopupK2Rank,mapPopupK2RankOptions,mapCompareK2RankOptions,mapPopupWilderView,mapPopupTab,mapPopupTitle,mapSetPopupTab,
      toggleMapStnHighlight,clearMapStnHighlights,
      // sample merging (cross-sample)
      mapMergeSelected,mapMergeResult,mapMergeLoading,mapMergeError,mapMergeView,
      mapToggleMergeSample,mapMergeSelectAll,mapClearMerge,mapMergeSamples,
      mapMergeTagFor,mapMergedTableRows,mapSetMergeView,
      // map compare
      mapCompareOpen,mapCompareView,mapCompareTopN,mapCompareK2Rank,mapCompareGroup,
      mapCompareSvg,mapCompareWrap,mapCompareLegend,
      selectedStationObjects,drawMapCompare,toggleMapCompare,mapStationKey,
    };
  }
});
// Keep a single component error from unmounting the whole app (white screen).
// The error is logged with full context so it can be diagnosed from the console.
MBON_APP.config.errorHandler = (err, instance, info) => {
  console.error('[MBON] Vue error:', err, '\n  during:', info, err && err.stack ? '\n' + err.stack : '');
};
MBON_APP.mount('#app');
