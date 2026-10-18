// A real external Node descendant, deliberately born after the initial snapshot.
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const [ffmpeg, output] = process.argv.slice(2);
fs.writeFileSync(path.join(output, 'worker.pid'), String(process.pid));
while (!fs.existsSync(path.join(output, 'spawn-ffmpeg.signal'))) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 20);
}
const child = spawn(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-re', '-f',
  'lavfi', '-i', 'sine=frequency=440', '-c:a', 'pcm_s16le', '-y',
  path.join(output, 'writing.wav')], {
    stdio: ['ignore', 'ignore', fs.openSync(path.join(output, 'ffmpeg.log'), 'a')],
    windowsHide: true,
    // libuv normally places non-detached children in a kill-on-close job.
    // Deliberately leave a survivor to exercise the installer's orphan drain.
    detached: true,
  });
child.once('spawn', () => {
  fs.writeFileSync(path.join(output, 'ffmpeg.pid'), String(child.pid));
  child.unref();
  process.exit(0);
});
child.once('error', error => {
  fs.writeFileSync(path.join(output, 'spawn-error.log'), String(error));
  process.exit(1);
});
