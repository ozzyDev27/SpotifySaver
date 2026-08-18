import os
import signal
import subprocess
import sys
import time
import urllib.request

root = os.path.dirname(os.path.abspath(__file__))
python = os.path.join(root, ".venv", "bin", "python")
server = os.path.join(root, "web", "server.py")
log_path = "/tmp/spotweb.log"

subprocess.run(["pkill", "-f", "web/server.py"], capture_output=True)
time.sleep(1)

log = open(log_path, "a")
proc = subprocess.Popen([python, server], stdout=log, stderr=log, cwd=root)

for _ in range(20):
	time.sleep(0.5)
	try:
		with urllib.request.urlopen("http://127.0.0.1:5510/", timeout=2) as r:
			if r.status == 200:
				break
	except Exception:
		pass
else:
	print("server failed to start, log tail:")
	subprocess.run(["tail", "-20", log_path])
	sys.exit(1)

print(f"running at http://127.0.0.1:5510  (pid {proc.pid}, log {log_path})")
print("ctrl-c to stop, or leave it running")
try:
	subprocess.run(["tail", "-f", log_path])
except KeyboardInterrupt:
	proc.send_signal(signal.SIGTERM)
	print("\nstopped")
