import {test} from 'node:test';
import assert from 'node:assert/strict';
import {consumeAnalysisStream} from '../src/payments_analyst/static/analysis-stream.js';
const stream=chunks=>new ReadableStream({start(controller){for(const chunk of chunks)controller.enqueue(new TextEncoder().encode(chunk));controller.close();}});
test('stream delivers actual stages and final answer across chunk boundaries',async()=>{
 const steps=[];const text='data: {"type":"step","step":{"id":"query","status":"running"}}\r\n\r\ndata: {"type":"result","data":{"answer":"₹25"}}\n\n';
 const result=await consumeAnalysisStream(stream([text.slice(0,20),text.slice(20,65),text.slice(65)]),step=>steps.push(step));
 assert.equal(steps.length,1);assert.equal(result.answer,'₹25');
});
test('stream never substitutes a result after a server failure',async()=>{
 await assert.rejects(consumeAnalysisStream(stream(['data: {"type":"error","status":409,"detail":"Data blocked"}\n\n'])),/Data blocked/);
 await assert.rejects(consumeAnalysisStream(stream(['data: {"type":"step","step":{}}\n\n'])),/ended before/);
});
