//! Canonical event model shared by every adapter and by the wire protocol.
//!
//! This mirrors `src/research_memory_gateway/ingest/schema.py` and
//! `schemas/conversation-ingest-v1.json`.  Provider vocabulary (Codex
//! `agent-turn-complete`, Claude Code `UserPromptSubmit`, ...) is mapped onto
//! this fixed set *inside the adapter*, so the gateway never has to know which
//! agent produced an event.

use serde::{Deserialize, Serialize};

/// Wire protocol version.  Must match `INGEST_SCHEMA_VERSION` on the gateway.
pub const SCHEMA_VERSION: i64 = 1;

/// Canonical event types accepted by the gateway.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum EventType {
    SessionStart,
    UserPrompt,
    AssistantMessage,
    SystemMessage,
    ToolCall,
    ToolResult,
    TurnEnd,
    SessionEnd,
}

impl EventType {
    pub const ALL: [EventType; 8] = [
        EventType::SessionStart,
        EventType::UserPrompt,
        EventType::AssistantMessage,
        EventType::SystemMessage,
        EventType::ToolCall,
        EventType::ToolResult,
        EventType::TurnEnd,
        EventType::SessionEnd,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            EventType::SessionStart => "session_start",
            EventType::UserPrompt => "user_prompt",
            EventType::AssistantMessage => "assistant_message",
            EventType::SystemMessage => "system_message",
            EventType::ToolCall => "tool_call",
            EventType::ToolResult => "tool_result",
            EventType::TurnEnd => "turn_end",
            EventType::SessionEnd => "session_end",
        }
    }

    pub fn parse(value: &str) -> Option<EventType> {
        EventType::ALL
            .iter()
            .copied()
            .find(|item| item.as_str() == value)
    }

    /// Event types that form the human-visible transcript.
    pub fn is_transcript(self) -> bool {
        matches!(
            self,
            EventType::UserPrompt | EventType::AssistantMessage | EventType::SystemMessage
        )
    }
}

impl std::fmt::Display for EventType {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Conversation role.  Empty is allowed for non-message events.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Role {
    User,
    Assistant,
    System,
    Tool,
    None,
}

impl Role {
    pub fn as_str(self) -> &'static str {
        match self {
            Role::User => "user",
            Role::Assistant => "assistant",
            Role::System => "system",
            Role::Tool => "tool",
            Role::None => "",
        }
    }

    pub fn parse(value: &str) -> Option<Role> {
        match value.trim().to_ascii_lowercase().as_str() {
            "user" | "human" => Some(Role::User),
            "assistant" | "agent" | "model" => Some(Role::Assistant),
            "system" | "developer" => Some(Role::System),
            "tool" | "function" => Some(Role::Tool),
            "" => Some(Role::None),
            _ => None,
        }
    }

    pub fn from_event_type(event_type: EventType) -> Role {
        match event_type {
            EventType::UserPrompt => Role::User,
            EventType::AssistantMessage => Role::Assistant,
            EventType::SystemMessage => Role::System,
            EventType::ToolCall | EventType::ToolResult => Role::Tool,
            _ => Role::None,
        }
    }
}

impl std::fmt::Display for Role {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One normalized event, exactly as it travels on the wire.
///
/// Field order and names match the JSON Schema; empty optional strings are
/// still serialized because the gateway schema declares them (with defaults),
/// and `extra = "forbid"` on the server side means we must not invent fields.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NormalizedEvent {
    pub event_id: String,
    pub schema_version: i64,
    pub source_system: String,
    #[serde(default)]
    pub source_account_namespace: String,
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub conversation_id: String,
    #[serde(default)]
    pub thread_id: String,
    #[serde(default)]
    pub branch_id: String,
    #[serde(default)]
    pub message_id: String,
    #[serde(default)]
    pub turn_id: String,
    pub event_type: String,
    #[serde(default)]
    pub role: String,
    #[serde(default)]
    pub content: String,
    #[serde(default)]
    pub timestamp: String,
    #[serde(default = "empty_object")]
    pub metadata: serde_json::Value,
}

fn empty_object() -> serde_json::Value {
    serde_json::Value::Object(serde_json::Map::new())
}

impl NormalizedEvent {
    /// Builder used by every adapter, so no adapter can forget a required field.
    pub fn new(
        source_system: impl Into<String>,
        event_type: EventType,
        content: impl Into<String>,
    ) -> NormalizedEvent {
        NormalizedEvent {
            event_id: String::new(),
            schema_version: SCHEMA_VERSION,
            source_system: source_system.into(),
            source_account_namespace: String::new(),
            session_id: String::new(),
            conversation_id: String::new(),
            thread_id: String::new(),
            branch_id: String::new(),
            message_id: String::new(),
            turn_id: String::new(),
            event_type: event_type.as_str().to_string(),
            role: Role::from_event_type(event_type).as_str().to_string(),
            content: content.into(),
            timestamp: String::new(),
            metadata: empty_object(),
        }
    }

    pub fn with_session(mut self, session_id: impl Into<String>) -> Self {
        self.session_id = session_id.into();
        self
    }

    pub fn with_conversation(mut self, conversation_id: impl Into<String>) -> Self {
        self.conversation_id = conversation_id.into();
        self
    }

    pub fn with_thread(mut self, thread_id: impl Into<String>) -> Self {
        self.thread_id = thread_id.into();
        self
    }

    pub fn with_branch(mut self, branch_id: impl Into<String>) -> Self {
        self.branch_id = branch_id.into();
        self
    }

    pub fn with_message(mut self, message_id: impl Into<String>) -> Self {
        self.message_id = message_id.into();
        self
    }

    pub fn with_turn(mut self, turn_id: impl Into<String>) -> Self {
        self.turn_id = turn_id.into();
        self
    }

    pub fn with_timestamp(mut self, timestamp: impl Into<String>) -> Self {
        self.timestamp = timestamp.into();
        self
    }

    pub fn with_role(mut self, role: Role) -> Self {
        self.role = role.as_str().to_string();
        self
    }

    pub fn with_account_namespace(mut self, namespace: impl Into<String>) -> Self {
        self.source_account_namespace = namespace.into();
        self
    }

    pub fn with_metadata(mut self, metadata: serde_json::Value) -> Self {
        self.metadata = metadata;
        self
    }

    pub fn with_metadata_entry(mut self, key: &str, value: serde_json::Value) -> Self {
        if !self.metadata.is_object() {
            self.metadata = empty_object();
        }
        if let Some(map) = self.metadata.as_object_mut() {
            map.insert(key.to_string(), value);
        }
        self
    }

    /// Effective conversation id: an explicit `conversation_id` wins, otherwise
    /// the session id is the conversation.  Mirrors the gateway's behaviour.
    pub fn effective_conversation_id(&self) -> String {
        if !self.conversation_id.trim().is_empty() {
            self.conversation_id.clone()
        } else {
            self.session_id.clone()
        }
    }

    pub fn event_type_enum(&self) -> Option<EventType> {
        EventType::parse(&self.event_type)
    }

    /// Structural validation shared by `capture` and `snapshot` builders.
    ///
    /// Fail-closed: an event that cannot be represented in the protocol is not
    /// silently coerced, it is dropped with a reason the caller must log.
    pub fn validate(&self) -> Result<(), String> {
        if self.event_id.len() < 8 {
            return Err("event_id is empty or too short".to_string());
        }
        if self.source_system.trim().is_empty() {
            return Err("source_system is empty".to_string());
        }
        if self.event_type_enum().is_none() {
            return Err(format!("unsupported event_type: {}", self.event_type));
        }
        if Role::parse(&self.role).is_none() {
            return Err(format!("unsupported role: {}", self.role));
        }
        if !self.metadata.is_object() {
            return Err("metadata must be a JSON object".to_string());
        }
        Ok(())
    }

    pub fn message_type(&self) -> bool {
        matches!(
            self.event_type_enum(),
            Some(EventType::UserPrompt) | Some(EventType::AssistantMessage)
        )
    }
}
