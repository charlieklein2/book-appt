import asyncio
import base64
import json
import sys
import websockets
import ssl
import requests

from pydub import AudioSegment
import requests

import openai

import os
from dotenv import load_dotenv
load_dotenv()

DEEPGRAM_API_KEY = os.getenv('DEEPGRAM_API_KEY')

def deepgram_connect():
	auth = 'Token ' + DEEPGRAM_API_KEY

	extra_headers = {
		'Authorization': auth 
	}
	deepgram_ws = websockets.connect("wss://api.deepgram.com/v1/listen?encoding=mulaw&sample_rate=8000&channels=1&multichannel=false", extra_headers = extra_headers)
	return deepgram_ws

async def generate_response(prompt):
	instruction = """ 
	You are a call service worker that helps callers book medical appointments. 
	You should obtain a caller's name, the doctor they are looking to see, and the date they want to see the doctor.
	Once this information is obtained, please indicate the call should terminate.
	"""

	openai.api_key = os.getenv('OPENAI_API_KEY')
	response = openai.ChatCompletion.create(
		model = "gpt-4o",
		messages = [
			{"role": "system", "content": instruction},
			{
				"role": "user",
				"content": prompt
			}
		]
	)
	return response.choices[0].message.content

async def proxy(client_ws):
	outbox = asyncio.Queue()

	audio_cursor = 0.

	async with deepgram_connect() as deepgram_ws:

		async def twilio_sender(twilio_ws, text):
			print("twilio_sender started")

			# wait to receive the streamsid for this connection from one of Twilio's messages
			#streamsid = await streamsid_queue.get()
			global streamsid

			# make a Deepgram Aura TTS request specifying that we want raw mulaw audio as the output
			url = "https://api.deepgram.com/v1/speak?model=aura-asteria-en&encoding=mulaw&sample_rate=8000&container=none"
			headers = {
				"Authorization": "Token " + DEEPGRAM_API_KEY,
				"Content-Type": "application/json",
			}
			payload = {"text": text}
			tts_response = requests.post(url, headers=headers, json=payload)

			if tts_response.status_code == 200:
				raw_mulaw = tts_response.content

				# construct a Twilio media message with the raw mulaw (see https://www.twilio.com/docs/voice/twiml/stream#websocket-messages---to-twilio)
				media_message = {
					"event": "media",
					"streamSid": streamsid,
					"media": {"payload": base64.b64encode(raw_mulaw).decode("ascii")},
				}

				# send the TTS audio to the attached phonecall
				await twilio_ws.send(json.dumps(media_message))
		
		async def deepgram_sender(deepgram_ws):
			print('started deepgram sender')
			while True:
				chunk = await outbox.get()
				await deepgram_ws.send(chunk)

		async def deepgram_receiver(client_ws, deepgram_ws):
			print('started deepgram receiver')

			async for message in deepgram_ws:
				try:
					dg_json = json.loads(message)
					transcript = dg_json['channel']['alternatives'][0]['transcript']
					if transcript != "":
						print(transcript)
						agent_response = await generate_response(transcript)
						print(agent_response)
						await twilio_sender(client_ws, agent_response)

				except:
					print('was not able to parse deepgram response as json')
					continue
			print('finished deepgram receiver')

		async def client_receiver(client_ws):
			print('started client receiver')
			nonlocal audio_cursor

			# we will use a buffer of 20 messages (20 * 160 bytes, 0.4 seconds) to improve throughput performance
			# NOTE: twilio seems to consistently send media messages of 160 bytes
			BUFFER_SIZE = 20 * 160
			buffer = bytearray(b'')
			empty_byte_received = False

			async for message in client_ws:
				try:
					data = json.loads(message)
					if data["event"] == "connected":
						print("Media WS: Received event connected")
						continue
					if data["event"] == "start":
						print("Media WS: Received event start")
						try:
							data = json.loads(message)

							if data["event"] == "start":
								global streamsid
								streamsid = data["start"]["streamSid"]
					
							
							await twilio_sender(client_ws, "Hello, how can I help you today?")
						except:
							break
						continue
					if data["event"] == "media":
						media = data["media"]
						chunk = base64.b64decode(media["payload"])
						time_increment = len(chunk) / 8000.0
						audio_cursor += time_increment
						buffer.extend(chunk)
						if chunk == b'':
							empty_byte_received = True
					if data["event"] == "stop":
						print("Media WS: Received event stop")
						break

					# check if our buffer is ready to send to our outbox (and, thus, then to deepgram)
					if len(buffer) >= BUFFER_SIZE or empty_byte_received:
						outbox.put_nowait(buffer)
						buffer = bytearray(b'')
				except:
					print('message from client not formatted correctly, bailing')
					break

			# if the empty byte was received, the async for loop should end, and we should here forward the empty byte to deepgram
			# or, if the empty byte was not received, but the WS connection to the client (twilio) died, then the async for loop will end and we should forward an empty byte to deepgram
			outbox.put_nowait(b'')
			print('finished client receiver')

		await asyncio.wait([
			asyncio.ensure_future(deepgram_sender(deepgram_ws)),
			asyncio.ensure_future(deepgram_receiver(client_ws, deepgram_ws)),
			asyncio.ensure_future(client_receiver(client_ws)), 
		
		])

		client_ws.close()
		print('finished running the proxy')


def main():
    server = websockets.serve(proxy, "localhost", 5000)

    asyncio.get_event_loop().run_until_complete(server)
    asyncio.get_event_loop().run_forever()


if __name__ == "__main__":
    sys.exit(main() or 0)