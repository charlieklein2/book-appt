# Callable AI Voice Agent
### Overview
This project synthesizes various software services to build a callable AI voice agent to simulate booking an appointment. To demo this project, call +1(844)823-0395.
### Design
* Telephone Service: Twilio phone number configured to handle incoming calls
* Speech-to-text: Real-time speech recognition with Deepgram STT model
* Language Model: Respond to user input to obtain booking and contact information
* Text-to-speech: Relay language model's output back to Twilio phone call with Deepgram TTS model
* Confirmation email: Follow-up email sent to caller summarizing booking information with SendGrid
