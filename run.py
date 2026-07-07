import subprocess
import time
import signal

print("Starting LiveKit worker...")

worker = subprocess.Popen(
    ["python", "telephony_agent.py", "dev"]
)

# Give the worker time to register
time.sleep(5)

print("Starting outbound call...")

try:
    subprocess.run(["python", "call.py"], check=True)
finally:
    print("Stopping worker...")
    worker.send_signal(signal.SIGINT)
    worker.wait()