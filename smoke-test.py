"""Run in WSL after llama reports ready. Exercises text, SSE usage, tools and tool continuation."""
import json
import urllib.request

URL = 'http://127.0.0.1:8080/v1/chat/completions'

def call(body):
    body = {'model': 'qwen3.8-27b', 'max_tokens': 512,
            'chat_template_kwargs': {'enable_thinking': False}, **body}
    req = urllib.request.Request(URL, json.dumps(body).encode(),
                                 {'Content-Type': 'application/json'})
    return urllib.request.urlopen(req, timeout=900)

with call({'messages': [{'role': 'user', 'content': 'Reply with OK.'}]}) as r:
    result = json.load(r)
assert result.get('usage'), result
print('TEXT:', result['choices'][0]['message'])
print('USAGE:', result['usage'])

usage = None
with call({'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
           'stream': True, 'stream_options': {'include_usage': True}}) as r:
    for line in r:
        line = line.decode().strip()
        if line.startswith('data: ') and line != 'data: [DONE]':
            chunk = json.loads(line[6:])
            if chunk.get('usage'):
                usage = chunk['usage']
assert usage, 'Missing final streaming usage'
print('STREAM USAGE:', usage)

messages = [{'role': 'user', 'content': 'Use get_number to fetch a number, then report it.'}]
tool = {'type': 'function', 'function': {'name': 'get_number',
        'description': 'Return a number.',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}}
with call({'messages': messages, 'tools': [tool],
           'tool_choice': {'type': 'function', 'function': {'name': 'get_number'}}}) as r:
    result = json.load(r)
message = result['choices'][0]['message']
assert message.get('tool_calls'), result
messages.append(message)
for t in message['tool_calls']:
    json.loads(t['function']['arguments'])
    messages.append({'role': 'tool', 'tool_call_id': t['id'], 'content': '{"number": 42}'})
with call({'messages': messages, 'tools': [tool], 'tool_choice': 'none'}) as r:
    result = json.load(r)
assert '42' in (result['choices'][0]['message'].get('content') or ''), result
print('TOOL ROUND TRIP OK')
