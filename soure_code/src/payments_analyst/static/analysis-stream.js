export async function consumeAnalysisStream(stream,onStep=()=>{}){
  const reader=stream.getReader(),decoder=new TextDecoder();
  let buffer='',result;
  function consume(frame){
    const data=frame.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');
    if(!data)return;
    const event=JSON.parse(data);
    if(event.type==='step')onStep(event.step);
    else if(event.type==='result')result=event.data;
    else if(event.type==='error'){const error=new Error(event.detail||'Analysis could not complete.');error.status=event.status;throw error;}
  }
  try{for(;;){const {done,value}=await reader.read();buffer+=(done?decoder.decode():decoder.decode(value,{stream:true}));buffer=buffer.replace(/\r\n/g,'\n');
    let boundary;while((boundary=buffer.indexOf('\n\n'))!==-1){consume(buffer.slice(0,boundary));buffer=buffer.slice(boundary+2);}
    if(done){if(buffer.trim())consume(buffer);break;}
  }}finally{reader.releaseLock();}
  if(result===undefined)throw new Error('The connection ended before the analysis completed. Please try again.');
  return result;
}
