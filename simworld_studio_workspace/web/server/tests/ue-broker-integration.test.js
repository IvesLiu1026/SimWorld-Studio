'use strict';
/* Integration test: real UeMcpBroker._execOnce against a MOCK UE MCP TCP server.
 * Verifies the one-shot wire protocol (JSON+\n -> JSON) and that the broker
 * serializes traffic (the mock never sees 2 concurrent connections).
 * Run: node tests/ue-broker-integration.test.js */

const net = require('net');
const PORT = 55991;
process.env.UNREAL_HOST = '127.0.0.1';
process.env.UNREAL_PORT = String(PORT);
process.env.UE_GATE_BUCKET_CAP = '1000';
process.env.UE_GATE_BUCKET_RATE = '1000';

const { UeMcpBroker } = require('../unreal-bridge');

let failures = 0;
const check = (n, c) => { console.log(`${c ? 'PASS' : 'FAIL'}: ${n}`); if (!c) failures++; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let liveConns = 0, maxConns = 0, served = 0;
const server = net.createServer((sock) => {
  liveConns++; maxConns = Math.max(maxConns, liveConns);
  let buf = '';
  sock.on('data', (d) => {
    buf += d.toString();
    const nl = buf.indexOf('\n');
    if (nl >= 0) {
      let req = {}; try { req = JSON.parse(buf.slice(0, nl)); } catch {}
      served++;
      // Simulate UE doing a little work, then reply with JSON and close.
      setTimeout(() => { sock.write(JSON.stringify({ status: 'success', type: req.type, params: req.params })); sock.end(); }, 10);
    }
  });
  sock.on('close', () => { liveConns--; });
  sock.on('error', () => {});
});

(async () => {
  await new Promise((r) => server.listen(PORT, '127.0.0.1', r));
  const b = new UeMcpBroker(); // real _execOnce against the mock

  // Burst of mixed commands through the real TCP path.
  const types = ['get_actors_in_level', 'spawn_actor', 'find_actors_by_name', 'execute_python_script', 'delete_actor', 'take_screenshot'];
  const results = await Promise.all(types.map((t, i) => b.send(t, { i }).then((r) => ({ t, ok: r && r.status === 'success', echo: r && r.type }))));

  check('integration all commands succeeded over real TCP', results.every((r) => r.ok));
  check('integration responses echo correct type', results.every((r) => r.echo === r.t));
  check('integration mock served all ' + types.length, served === types.length);
  check('integration broker serialized (mock saw <=1 concurrent conn, saw ' + maxConns + ')', maxConns === 1);
  check('integration broker.totalSent == ' + types.length, b.status().totalSent === types.length);

  server.close();
  await sleep(20);
  console.log(failures === 0 ? '\nALL INTEGRATION TESTS PASSED' : `\n${failures} FAILED`);
  process.exit(failures === 0 ? 0 : 1);
})();
