import assert from 'node:assert/strict'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

// Compile the real client with Vite's existing compiler; no browser or live API is used.
const compiled = await build({
  entryPoints: [fileURLToPath(new URL('../src/api/index.ts', import.meta.url))],
  bundle: true, write: false, platform: 'node', format: 'cjs',
  plugins: [{
    name: 'isolated-auth-client',
    setup(builder) {
      builder.onResolve({ filter: /^\.\/client$/ }, () => ({ path: 'client', namespace: 'test' }))
      builder.onLoad({ filter: /.*/, namespace: 'test' }, () => ({
        contents: 'export default {}; export async function refreshAccessToken() { return null }',
      }))
    },
  }],
})

function clientWithResponse(chunks) {
  const encoder = new TextEncoder()
  const body = new ReadableStream({ start(controller) {
    for (const chunk of chunks) controller.enqueue(typeof chunk === 'string' ? encoder.encode(chunk) : chunk)
    controller.close()
  } })
  const requests = []
  const module = { exports: {} }
  runInNewContext(compiled.outputFiles[0].text, {
    module, exports: module.exports, TextDecoder,
    localStorage: { getItem: () => 'test-token' },
    fetch: async (url, options) => {
      requests.push({ url, ...options })
      return new Response(body, { status: 200 })
    },
  })
  return { chatStream: module.exports.chatStream, requests, body }
}

test('SSE preserves split UTF-8, CRLF and a final event without a blank line', async () => {
  const bytes = new TextEncoder().encode(': heartbeat\r\ndata: {"type":"token","content":"你好"}\r\n\r\ndata: {"type":"done"}\n')
  const client = clientWithResponse(Array.from(bytes, byte => new Uint8Array([byte])))
  const events = []
  await client.chatStream('session-a', 'question', event => events.push(event), undefined, 'project-a')
  assert.equal(events.length, 2)
  assert.equal(events[0].content, '你好')
  assert.equal(events[1].type, 'done')
  assert.deepEqual(JSON.parse(client.requests[0].body), { session_id: 'session-a', message: 'question', project_id: 'project-a' })
  assert.equal(client.body.locked, false)
})

test('multi-line event data retains the capability guidance', async () => {
  const client = clientWithResponse(['data: {"type":"guidance",\n', 'data: "capability":"procurement"}\n\n'])
  const events = []
  await client.chatStream('s', 'q', event => events.push(event))
  assert.equal(events[0].capability, 'procurement')
})

test('malformed data and missing event type fail explicitly', async () => {
  for (const data of ['not-json', '{"content":"missing type"}']) {
    const client = clientWithResponse([`data: ${data}\n\n`])
    await assert.rejects(client.chatStream('s', 'q', () => {}), /对话事件/)
    assert.equal(client.body.locked, false)
  }
})

test('callback failures are not swallowed as JSON errors', async () => {
  const client = clientWithResponse(['data: {"type":"token","content":"test"}\n\n'])
  await assert.rejects(client.chatStream('s', 'q', () => { throw new Error('render failed') }), /render failed/)
  assert.equal(client.body.locked, false)
})

test('an empty successful response fails explicitly instead of leaving the UI thinking forever', async () => {
  const client = clientWithResponse([])
  await assert.rejects(client.chatStream('s', 'q', () => {}), /未返回任何对话结果/)
  assert.equal(client.body.locked, false)
})
