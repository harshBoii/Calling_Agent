"""Pydantic models for POST /call/outbound."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class VoiceConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ttsVoiceId: str | None = None
    languageAccent: str | None = None
    gender: str | None = None
    speakingPace: str | None = None


class PersonalityProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tone: str | None = None
    formality: str | None = None
    energy: str | None = None
    humorAllowance: str | None = None


class IdentityConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agentName: str | None = None
    companyName: str | None = None
    roleFraming: str | None = None
    voice: VoiceConfig | None = None
    personalityProfile: PersonalityProfile | None = None


class ConversationStage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    label: str
    goal: str
    exitCondition: str
    maxTurns: int = Field(default=2, ge=1)
    skipToTargets: list[str] = Field(default_factory=list)


class ConversationFlowConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    stages: list[ConversationStage] = Field(min_length=1)
    offScriptBehavior: Literal["redirect", "allow"] = "redirect"


class KnowledgeGrounding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    unknownFactFallbackLine: str | None = None
    claimWhitelist: list[str] = Field(default_factory=list)
    claimBlacklist: list[str] = Field(default_factory=list)


class EscalationTrigger(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal[
        "anger_detected",
        "explicit_human_request",
        "failed_objection_handles",
    ]
    threshold: int | None = None


class BehavioralGuardrails(BaseModel):
    model_config = ConfigDict(extra="ignore")

    maxSentencesPerTurn: int = Field(default=2, ge=1)
    escalationTriggers: list[EscalationTrigger] = Field(default_factory=list)


class ObjectionHandling(BaseModel):
    model_config = ConfigDict(extra="ignore")

    objectionLibrary: dict[str, str] = Field(default_factory=dict)
    maxObjectionAttempts: int = Field(default=2, ge=1)
    softCloseLine: str | None = None


class BookingClose(BaseModel):
    model_config = ConfigDict(extra="ignore")

    calendarSourceId: str | None = None
    slotsToOffer: int = Field(default=2, ge=1)
    closeTrigger: str | None = None
    bookingConfirmationMethod: str | None = None


class Personalization(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabledPresetIds: list[str] = Field(default_factory=list)


class Compliance(BaseModel):
    model_config = ConfigDict(extra="ignore")

    aiDisclosureLine: str | None = None
    maxCallDurationSec: int | None = Field(default=None, ge=30)
    optOutPhrase: str | None = None
    optOutSetsDncFlag: bool = True


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    identity: IdentityConfig | None = None
    conversationFlow: ConversationFlowConfig | None = None
    knowledgeGrounding: KnowledgeGrounding | None = None
    behavioralGuardrails: BehavioralGuardrails | None = None
    objectionHandling: ObjectionHandling | None = None
    bookingClose: BookingClose | None = None
    personalization: Personalization | None = None
    compliance: Compliance | None = None


class OutboundCallRequest(BaseModel):
    """Validated body for POST /call/outbound."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    to: str
    call_type: Literal["on_demand", "campaign"] = "campaign"
    companyId: str | None = None
    leadId: str | None = None
    campaignId: str | None = None
    name: str | None = None
    company: str | None = None
    product: str | None = None
    perks_of_product: str | None = None
    info_about_lead: str | None = None
    agent_name: str | None = None
    agent_role: str | None = None
    language: str | None = None
    languageMode: str | None = None
    deepgram_language: str | None = None
    deepgramLanguage: str | None = None
    elevenlabs_model: str | None = None
    voiceMode: str | None = None
    voiceId: str | None = None
    questions_to_ask: str | list[str] | None = None
    QUESTIONS_TO_ASK: str | list[str] | None = None
    questions: str | list[str] | None = None
    system_prompt: str | None = None
    opening_greeting: str | None = None
    dynamic_greeting: bool = True
    previousChatContext: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    stt_provider: str | None = None
    use_sarvam_tts: bool = False
    sarvam_speaker: str | None = None
    agent_config: AgentConfig | None = None

    def to_cfg_body(self) -> dict[str, Any]:
        data = self.model_dump(exclude={"to"}, exclude_none=False)
        if self.agent_config is not None:
            data["agent_config"] = self.agent_config.model_dump()
        return data
