"""Start Bifrost with bifrost/config.json, passing provider keys from .env. Usage: python run_bifrost.py [port]"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

root = Path(__file__).resolve().parent
app_dir = root / "build" / "bifrost"
app_dir.mkdir(parents=True, exist_ok=True)
shutil.copy(root / "bifrost" / "config.json", app_dir / "config.json")
env = dict(os.environ)
env.update({k: v for k, v in dotenv_values(root / ".env").items() if v})
port = sys.argv[1] if len(sys.argv) > 1 else "8080"
subprocess.run(f'npx -y @maximhq/bifrost -port {port} -app-dir "{app_dir}"', shell=True, env=env)
