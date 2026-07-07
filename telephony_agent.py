import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.plugins import cartesia, deepgram, openai, silero

from scenarios import build_patient_prompt

load_dotenv()

logger = logging.getLogger("telephony-agent")

# Default Patient if Patient Prompt doesn't load properly
# Mostly so that the try catch block doesn't have issues
DEFAULT_PATIENT = {
    "id": "default_schedule",
    "name": "Emily Parker",
    "age": 34,
    "goal": "Schedule a new patient appointment.",
    "reason": "sore throat and congestion for five days",
    "availability": "weekday afternoons",
    "insurance": "Blue Cross Blue Shield",
    "personality": "friendly and slightly anxious",
    "extra_details": "You are a first-time patient.",
}


@function_tool
async def get_current_time() -> str:
    """Get the current time."""
    return f"The current time is {datetime.now().strftime('%I:%M %p')}"


def get_text_from_item(item) -> str:
    """Extract text from a LiveKit conversation item across SDK versions."""
    if hasattr(item, "text_content") and item.text_content:
        return item.text_content

    content = getattr(item, "content", "")
    if isinstance(content, list):
        return " ".join(str(part) for part in content if part)
    return str(content or "")


# Checks to see if the office is ready
def is_office_ready(text: str) -> bool:
    """
    Return True only when the remote office has moved past the recording
    disclosure and is actually asking the caller what they need.
    """
    text = text.lower().strip()

    ignored_phrases = [
        "this call may be recorded",
        "recorded for quality",
        "quality and training",
        "training purposes",
    ]
    if any(phrase in text for phrase in ignored_phrases):
        return False

    ready_phrases = [
        "how can i help",
        "how may i help",
        "how can i assist",
        "how may i assist",
        "what can i help",
        "what can i do for you",
        "how can we help",
        "how may we help",
    ]
    return any(phrase in text for phrase in ready_phrases)


# Phone service initialized
async def entrypoint(ctx: JobContext):
    await ctx.connect()

    participant = await ctx.wait_for_participant()
    logger.info("Phone call connected from participant: %s", participant.identity)

    metadata = (
        getattr(ctx.job, "metadata", None)
        or getattr(ctx.room, "metadata", None)
        or "{}"
    )
    logger.info("Raw metadata received: %s", metadata)

    try:
        patient = json.loads(metadata)
    except json.JSONDecodeError:
        logger.warning("Could not parse patient metadata. Falling back to default scenario.")
        patient = DEFAULT_PATIENT

    logger.info("Loaded patient scenario: %s", patient.get("id"))

    patient_prompt = build_patient_prompt(patient)
    logger.info("Patient prompt:\n%s", patient_prompt)

    agent = Agent(
        instructions=patient_prompt,
        tools=[get_current_time],
    )

    # Initialize Deepgram for Speech to Text
    # Initialize OpenAI for LLM
    # Initialize Cartesia for Text to Speech
    # The whole stack is what enables for the voice agent to turn the remote agent's 
    # words into text, process it, and spit the result back out as text.
    session = AgentSession(
        vad=silero.VAD.load(),
        stt=deepgram.STT(
            model="nova-3",
            language="en-US",
            interim_results=True,
            punctuate=True,
            smart_format=True,
            filler_words=True,
            endpointing_ms=25,
            sample_rate=16000,
        ),
        llm=openai.LLM(
            model="gpt-4o-mini",
            temperature=0.7,
        ),
        tts=cartesia.TTS(
            model="sonic-2",
            voice="a0e99841-438c-4a64-b679-ae501e7d6091",
            language="en",
            speed=1.0,
            sample_rate=24000,
        ),
    )

    # Creates a transcript folder if it doesn't already exist
    transcript_dir = Path("transcripts")
    transcript_dir.mkdir(exist_ok=True)
    transcript_path = transcript_dir / f"{ctx.room.name}.txt"

    # Creates the transcript file in the transcript folder for that call
    def write_transcript(role: str, text: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with transcript_path.open("a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {role}: {text}\n")

    # Speaking checks to see if the office started speaking before the patient begins speaking
    patient_has_started = False
    office_is_speaking = False
    office_quiet_event = asyncio.Event()
    office_quiet_event.set()

    @ctx.room.on("active_speakers_changed")
    def on_active_speakers_changed(speakers):
        nonlocal office_is_speaking

        remote_is_speaking = any(
            getattr(speaker, "identity", None) == participant.identity
            for speaker in speakers
        )

        if remote_is_speaking:
            office_is_speaking = True
            office_quiet_event.clear()
        else:
            if office_is_speaking:
                office_is_speaking = False

                async def mark_quiet_after_buffer():
                    await asyncio.sleep(0.9)
                    if not office_is_speaking:
                        office_quiet_event.set()

                asyncio.create_task(mark_quiet_after_buffer())

    # Conversation start check done here
    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        nonlocal patient_has_started

        item = event.item
        role = getattr(item, "role", "unknown")
        text = get_text_from_item(item).strip()

        if text:
            write_transcript(role, text)

        if role == "user" and not patient_has_started and is_office_ready(text):
            patient_has_started = True

            async def start_patient_after_office_finishes():
                try:
                    await asyncio.wait_for(office_quiet_event.wait(), timeout=4.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timed out waiting for office silence; starting patient anyway."
                    )
                # This 0.4 second wait is after the pre-recorded message
                await asyncio.sleep(0.4)

                # Specifics so that the patient does not start behaving like a different agent
                await session.generate_reply(
                    instructions="""
                    The office has finished asking how they can help.

                    Respond as the patient.
                    Do not act like the receptionist.
                    Do not say "thank you for calling."
                    Do not ask "how can I help you?"
                    Do not mention that you were waiting.

                    Start with your reason for calling.
                    """
                )

            asyncio.create_task(start_patient_after_office_finishes())

    await session.start(agent=agent, room=ctx.room)

    logger.info(
        "Patient agent started silently. Waiting for office greeting and audio silence."
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="telephony_agent",
        )
    )
