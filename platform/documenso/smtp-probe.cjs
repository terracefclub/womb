// Synthetic VM acceptance only: no message, recipient, credential or external host.
const dns = require('node:dns').promises;
const net = require('node:net');

let stage = 'configuration';
let socket;
let transport;
const codes = new Set(['ENOTFOUND', 'ECONNREFUSED', 'ETIMEDOUT', 'EHOSTUNREACH', 'ENETUNREACH', 'EDNS', 'ECONNECTION', 'EAUTH', 'MODULE_NOT_FOUND']);
const deadline = setTimeout(() => {
  socket?.destroy();
  transport?.close();
  console.error(`Synthetic container SMTP probe failed at ${stage}: deadline.`);
  process.exit(1);
}, 7000);

(async () => {
  if (process.env.NEXT_PRIVATE_SMTP_HOST !== '10.88.0.1'
      || process.env.NEXT_PRIVATE_SMTP_PORT !== '1025'
      || process.env.NEXT_PRIVATE_SMTP_SECURE !== 'false'
      || process.env.NEXT_PRIVATE_SMTP_UNSAFE_IGNORE_TLS !== 'true'
      || process.env.NEXT_PRIVATE_JOBS_PROVIDER !== 'local'
      || process.env.NEXT_PRIVATE_INTERNAL_WEBAPP_URL !== 'http://127.0.0.1:3000'
      || process.env.NEXT_PRIVATE_SMTP_TRANSPORT !== 'smtp-auth'
      || process.env.NEXT_PRIVATE_SMTP_USERNAME || process.env.NEXT_PRIVATE_SMTP_SERVICE) throw Error();
  stage = 'host-alias';
  const addresses = await dns.lookup('host.containers.internal', { all: true });
  if (!addresses.length || !addresses.every(row => row.family === 4 && row.address === '10.88.0.1')) throw Error();
  stage = 'protocol';
  await new Promise((resolve, reject) => {
    let buffer = '';
    let phase = 0;
    socket = net.createConnection({ host: addresses[0].address, port: 1025 });
    socket.setEncoding('ascii');
    socket.on('error', reject);
    socket.on('close', () => { if (phase !== 3) reject(Error()); });
    socket.on('data', chunk => {
      buffer += chunk;
      if (buffer.length > 8192) { reject(Error()); return; }
      while (buffer.includes('\r\n')) {
        const end = buffer.indexOf('\r\n');
        const line = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        if (!/^[0-9]{3}[- ]/.test(line) || Number(line.slice(0, 3)) !== [220, 250, 221][phase]) {
          reject(Error()); return;
        }
        if (line[3] === '-') continue;
        phase += 1;
        if (phase === 1) socket.write('EHLO synthetic.terrace.invalid\r\n');
        else if (phase === 2) socket.write('QUIT\r\n');
        else { socket.destroy(); resolve(); }
      }
    });
  });
  stage = 'native-mailer';
  // Use the official image's installed library and the same options as its
  // pinned packages/email/mailer.ts. No username means no SMTP authentication.
  transport = require('/app/node_modules/nodemailer').createTransport({
    host: process.env.NEXT_PRIVATE_SMTP_HOST,
    port: Number(process.env.NEXT_PRIVATE_SMTP_PORT),
    secure: false, ignoreTLS: true,
  });
  if (await transport.verify() !== true) throw Error();
  transport.close();
  clearTimeout(deadline);
  console.log('Native container SMTP alias, protocol and image mailer verified without sending mail.');
})().catch(error => {
  clearTimeout(deadline);
  socket?.destroy();
  transport?.close();
  const code = codes.has(error?.code) ? error.code : 'unspecified';
  console.error(`Synthetic container SMTP probe failed at ${stage}: ${code}.`);
  process.exitCode = 1;
});
