import asyncio
import base64
import json
import sys
import websockets
import requests
import os
from dotenv import load_dotenv
import openai

import smtplib
from email.message import EmailMessage

from twilio.rest import Client

load_dotenv()
DEEPGRAM_API_KEY = os.getenv('DEEPGRAM_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
DEEPGRAM_URL_STT = os.getenv('DEEPGRAM_URL_STT')
DEEPGRAM_URL_TTS = os.getenv('DEEPGRAM_URL_TTS')
INSTRUCTION_PROMPT_RESPONSE = os.getenv('INSTRUCTION_PROMPT_RESPONSE')
INSTRUCTION_PROMPT_SUMMARY = os.getenv('INSTRUCTION_PROMPT_SUMMARY')
SENDER_EMAIL = os.getenv('SENDER_EMAIL')
SENDGRID_API_KEY = os.getenv('SENDGRID_API_KEY')
EMAIL_CONFIRMATION_SUBJECT = os.getenv('EMAIL_CONFIRMATION_SUBJECT')
EMAIL_CONFIRMATION_CONTENT = os.getenv('EMAIL_CONFIRMATION_CONTENT')
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

def deepgram_connect():
	auth = 'Token ' + DEEPGRAM_API_KEY

	extra_headers = {
		'Authorization': auth 
	}
	deepgram_ws = websockets.connect(DEEPGRAM_URL_STT, extra_headers = extra_headers)
	return deepgram_ws

async def generate_response(prompt, entire_transcript):
	instruction = INSTRUCTION_PROMPT_RESPONSE

	openai.api_key = OPENAI_API_KEY
	response = openai.ChatCompletion.create(
		model = "gpt-4o",
		messages = [
			{"role": "system", "content": instruction + entire_transcript},
			{
				"role": "user",
				"content": prompt
			}
		]
	)
	
	return response.choices[0].message.content

async def generate_and_send_summary(entire_transcript):

	openai.api_key = OPENAI_API_KEY
	response = openai.ChatCompletion.create(
		model = "gpt-4o",
		messages = [
			{"role": "system", "content": INSTRUCTION_PROMPT_SUMMARY},
			{
				"role": "user",
				"content": entire_transcript
			}
		]
	)

	summary = response.choices[0].message.content

	response2 = openai.ChatCompletion.create(
		model = "gpt-4o",
		messages = [
			{"role": "system", "content": "Given the entire transcript, extract only the email address of the caller."},
			{
				"role": "user",
				"content": entire_transcript
			}
		]
	)

	receiver_email = response2.choices[0].message.content

	msg = EmailMessage()
	msg.set_content("Thank you for booking an appointment with this demo. The following is a summary of your booking:\n\n" + summary)
	msg['Subject'] = EMAIL_CONFIRMATION_SUBJECT
	msg['From'] = SENDER_EMAIL
	msg['To'] = receiver_email
	try:
		with smtplib.SMTP('smtp.sendgrid.net', 587) as server: 
			server.starttls()  # Upgrade the connection to secure
			server.login('apikey', SENDGRID_API_KEY)  
			server.send_message(msg) 
		print("Email sent successfully!")
	except Exception as e:
		print(f"Failed to send email: {e}")

	return summary

def end_call():
	client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
	call_sid = client.calls.list()[0]
	client.calls(call_sid).update(status="completed")

async def proxy(client_ws):
	outbox = asyncio.Queue()

	audio_cursor = 0.

	entire_transcript = ""

	async with deepgram_connect() as deepgram_ws:

		async def twilio_sender(twilio_ws, text):
			# wait to receive the streamsid for this connection from one of Twilio's messages
			#streamsid = await streamsid_queue.get()
			global streamsid

			# make a Deepgram Aura TTS request specifying that we want raw mulaw audio as the output
			url = DEEPGRAM_URL_TTS
			headers = {
				"Authorization": "Token " + DEEPGRAM_API_KEY,
				"Content-Type": "application/json",
			}
			payload = {"text": text}
			tts_response = requests.post(url, headers=headers, json=payload)

			if tts_response.status_code == 200:
				raw_mulaw = tts_response.content

				# construct a Twilio media message 
				media_message = {
					"event": "media",
					"streamSid": streamsid,
					"media": {"payload": base64.b64encode(raw_mulaw).decode("ascii")},
				}

				# send the TTS audio to the attached phonecall
				await twilio_ws.send(json.dumps(media_message))

				nonlocal entire_transcript
				entire_transcript += "agent: " + text + "\n"
		
		async def deepgram_sender(deepgram_ws):
			print('started deepgram sender')
			try:
				while True:
					chunk = await outbox.get()
					await deepgram_ws.send(chunk)
			except websockets.exceptions.ConnectionClosedOK:
				print("Deepgram WebSocket closed gracefully.")
			except Exception as e:
				print(f"Unexpected error in deepgram_sender: {e}")

		async def deepgram_receiver(client_ws, deepgram_ws):
			print('started deepgram receiver')
			nonlocal entire_transcript

			async for message in deepgram_ws:
				try:
					dg_json = json.loads(message)
					transcript = dg_json['channel']['alternatives'][0]['transcript']
					
					if transcript != "":
						entire_transcript += "user: " + transcript + "\n"
						print(transcript)
						agent_response = await generate_response(transcript, entire_transcript)
						print(agent_response)
						if agent_response != "end call":
							await twilio_sender(client_ws, agent_response)
						else:
							await twilio_sender(client_ws, "Thank you, have a good day!")
							print(await generate_and_send_summary(entire_transcript))
							
							### send signal to Twilio to end call
							try: 
								await deepgram_ws.close()
								await client_ws.close()
							except Exception as e:
								print(f"Error while closing WebSocket connections: {e}")

							end_call()

				except:
					print('was not able to parse deepgram response as json')
					continue

			print('finished deepgram receiver')

		async def client_receiver(client_ws):
			print('started client receiver')
			nonlocal audio_cursor

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
					
							await twilio_sender(client_ws, "Hello, this is your local doctors office, would you like to book an appointment?")
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

			outbox.put_nowait(b'')
			print('finished client receiver')

		await asyncio.wait([
			asyncio.ensure_future(deepgram_sender(deepgram_ws)),
			asyncio.ensure_future(deepgram_receiver(client_ws, deepgram_ws)),
			asyncio.ensure_future(client_receiver(client_ws)), 
		
		])

		await client_ws.close()
		print('finished running the proxy')


def main():
	server = websockets.serve(proxy, "localhost", 5000)

	asyncio.get_event_loop().run_until_complete(server)
	asyncio.get_event_loop().run_forever()


if __name__ == "__main__":
	sys.exit(main() or 0)