const solar={busy:false};const status=document.querySelector('#feedback');document.querySelector('#recompute').addEventListener('click',async()=>{if(solar.busy)return;solar.busy=true;status.textContent='复算中…';try{const dataset=document.querySelector('#dataset').value;const response=await fetch(dataset===document.body.dataset.source?'/api/runs/'+document.body.dataset.runId+'/rerun':'/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset})});const run=await response.json();if(!response.ok)throw Error(run.detail);const url=new URL(location.href);url.searchParams.set('run_id',run.manifest.run_id);url.searchParams.set('dataset',dataset);location.href=url;}catch(error){status.textContent=error.message;}finally{solar.busy=false;}});
const filePicker=document.querySelector('#csv-file');
if(filePicker){
 const editor=document.querySelector('#csv-text');
 filePicker.addEventListener('change',async()=>{const file=filePicker.files[0];if(!file)return;if(file.size>2000000){editor.value='';alert('文件超过2MB，请缩小后导入');return;}const bytes=await file.arrayBuffer();try{editor.value=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch(e){editor.value=new TextDecoder('gb18030',{fatal:true}).decode(bytes);}});
 document.querySelector('#import-csv').addEventListener('click',async event=>{event.target.disabled=true;try{const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csv_text:editor.value})});const data=await response.json();if(!response.ok)throw Error(data.detail);location.href=location.pathname+'?run_id='+data.manifest.run_id;}catch(e){editor.setCustomValidity(e.message);editor.reportValidity();}finally{event.target.disabled=false;}});
 editor.addEventListener('input',()=>editor.setCustomValidity(''));
 }
const basisForm=document.querySelector('#basis-form');
if(basisForm){
 const runId=document.body.dataset.runId;
 const feedback=document.querySelector('#basis-feedback');
 const basisRows=document.querySelector('#basis-rows');
 const refreshHistory=async()=>{
  try{
   const data=await fetch('/api/runs/'+runId+'/basis').then(r=>r.json());
   const entries=data.basis||[];
   basisRows.innerHTML=entries.length?entries.map(e=>'<tr><td>r'+e.revision+'</td><td>'+Object.entries(e.basis).map(([k,v])=>k+'='+v).join('，')+'</td><td>'+(e.note||'')+'</td><td title="'+e.sha256+'">'+e.sha256.slice(0,10)+'…</td></tr>').join(''):'<tr><td colspan="4">尚未提交上游依据，使用批次内默认限值。</td></tr>';
  }catch(e){basisRows.innerHTML='<tr><td colspan="4">依据历史读取失败。</td></tr>';}
 };
 refreshHistory();
 basisForm.addEventListener('submit',async event=>{
  event.preventDefault();
  const form=new FormData(basisForm);
  const basis={};
  for(const key of ['charge_kw','discharge_kw','export_limit_kw']){
   const v=String(form.get(key)||'').trim();
   if(v!=='')basis[key]=v;
  }
  if(!Object.keys(basis).length){feedback.textContent='请至少填写一项依据数值。';return;}
  const note=String(form.get('note')||'');
  const button=document.querySelector('#basis-submit');button.disabled=true;feedback.textContent='提交中…';
  try{
   const response=await fetch('/api/runs/'+runId+'/basis',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({basis,note})});
   const data=await response.json();
   if(!response.ok)throw Error(data.detail||'依据校验失败，已保留旧版');
   feedback.textContent=data.changed?('已生成 r'+data.basis_revision+'，重算 '+data.affected_segments+' 个受影响片段。'):('同一依据幂等重试：仍为 r'+data.basis_revision+'，未新增电量。');
   basisForm.reset();
   await refreshHistory();
   setTimeout(()=>location.reload(),900);
  }catch(e){feedback.textContent='切换失败，旧版依据未改动：'+e.message;}
  finally{button.disabled=false;}
 });
}
