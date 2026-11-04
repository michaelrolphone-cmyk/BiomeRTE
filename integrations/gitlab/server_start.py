#!/usr/bin/env python3
"""Bring the existing persistent GitLab container online at Mac user login."""
import shutil,subprocess,time
def main():
 docker=shutil.which("docker") or "/Applications/Docker.app/Contents/Resources/bin/docker"
 def ready():return subprocess.run([docker,"info"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
 if not ready():
  subprocess.run(["/usr/bin/open","-gja","Docker"],check=True)
  while not ready():time.sleep(3)
 subprocess.run([docker,"start","biome-gitlab"],check=True)
if __name__=="__main__":main()
