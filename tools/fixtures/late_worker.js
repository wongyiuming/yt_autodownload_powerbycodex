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
  path.join(output, 'writing.wav')], { stdio: 'ignore', windowsHide: true });
fs.writeFileSync(path.join(output, 'ffmpeg.pid'), String(child.pid));
// Leave a real FFmpeg orphan for the cleanup's NEXT descendant scan.
process.exit(0);
