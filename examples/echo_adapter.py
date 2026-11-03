"""Non-hardware adapter example. The worker sends one JSON request on stdin."""
import json
import sys
request = json.load(sys.stdin)
print(json.dumps({'value': request['inputs']['value']}))
