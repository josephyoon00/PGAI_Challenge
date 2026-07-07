import asyncio
import json
import os
import uuid

from dotenv import load_dotenv
from livekit import api

from scenarios import get_random_patient

load_dotenv()

# Global Variables Defined Here

LIVEKIT_URL = os.environ["LIVEKIT_URL"]
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]
LIVEKIT_SIP_TRUNK_ID = os.environ["LIVEKIT_SIP_TRUNK_ID"]

OUTBOUND_NUMBER = "+18054398008"

# Start audio recording to upload to AWS S3 Bucket
async def start_audio_recording(lkapi, room_name: str, scenario_id: str):
    egress = await lkapi.egress.start_room_composite_egress(
        api.RoomCompositeEgressRequest(
            room_name=room_name,
            audio_only=True,
            file_outputs=[
                api.EncodedFileOutput(
                    filepath=f"recordings/{scenario_id}/{room_name}.ogg",
                    s3=api.S3Upload(
                        bucket=os.environ["S3_BUCKET"],
                        region=os.environ["S3_REGION"],
                        access_key=os.environ["S3_ACCESS_KEY"],
                        secret=os.environ["S3_SECRET"],
                    ),
                )
            ],
        )
    )

    print(f"Audio recording started: {egress.egress_id}")
    return egress.egress_id

async def main():
    # Initialize Patient and Metadata for Patient from scenarios.py
    patient = get_random_patient()
    metadata = json.dumps(patient)
    scenario_id = patient["id"]

    room_name = f"pgai-{uuid.uuid4().hex[:8]}"

    # Initalize LiveKit for Calls
    lkapi = api.LiveKitAPI(
        LIVEKIT_URL,
        LIVEKIT_API_KEY,
        LIVEKIT_API_SECRET,
    )

    try:

        await lkapi.room.create_room(
            api.CreateRoomRequest(name=room_name)
        )

        print(f"Created room: {room_name}")
        print(f"Selected scenario: {patient['id']} - {patient['goal']}")

        # Initialize permissions for call
        

        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                room=room_name,
                agent_name="telephony_agent",
                metadata=metadata,
            )
        )

        # Connect patient to PGAI agent
        await lkapi.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=room_name,
                sip_trunk_id=LIVEKIT_SIP_TRUNK_ID,
                sip_call_to=OUTBOUND_NUMBER,
                participant_identity="pgai-test-line",
                participant_name="Pretty Good AI Test Line",
            )
        )

        egress_id = await start_audio_recording(
            lkapi=lkapi,
            room_name=room_name,
            scenario_id=scenario_id,
        )
        
        print(f"Calling {OUTBOUND_NUMBER}...")
        print(f"Room: {room_name}")
        print(f"Scenario: {patient['id']}")
        print(patient)
        await asyncio.sleep(180)

    finally:
        await lkapi.aclose()


if __name__ == "__main__":
    asyncio.run(main())