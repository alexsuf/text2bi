from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, UniqueConstraint, BigInteger, Numeric
from sqlalchemy.sql import func, text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from db import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "app"}

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    theme = Column(String(20), default="dark")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ConnectionSetting(Base):
    __tablename__ = "connection_settings"
    __table_args__ = {"schema": "app"}

    id = Column(Integer, primary_key=True, index=True)
    host = Column(String(255), nullable=False)
    port = Column(Integer, nullable=False, default=5432)
    database_name = Column(String(255), nullable=False)
    username = Column(String(255), nullable=False)
    password = Column(String(255), nullable=False)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SystemConfig(Base):
    __tablename__ = "system_config"
    __table_args__ = {"schema": "app"}

    key = Column(String(255), primary_key=True)
    value = Column(Text, nullable=False)
    description = Column(Text)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Prompt(Base):
    __tablename__ = "prompts"
    __table_args__ = {"schema": "app"}

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    prompt_key = Column(String(255), unique=True, nullable=False)
    category = Column(String(100), nullable=False, default="general")
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SavedQuery(Base):
    __tablename__ = "saved_queries"
    __table_args__ = {"schema": "app"}

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False)
    title = Column(String(255), nullable=False)
    query_text = Column(Text, nullable=False)
    connection_id = Column(Integer, ForeignKey("app.connection_settings.id", ondelete="CASCADE"), nullable=True)
    result_columns = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class QueryHistory(Base):
    __tablename__ = "query_history"
    __table_args__ = {"schema": "app"}

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("app.users.id", ondelete="CASCADE"), nullable=False)
    question = Column(Text, nullable=False)
    generated_sql = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PromptReport(Base):
    __tablename__ = "prompt_report"
    __table_args__ = {"schema": "app"}

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False)
    name = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ReportPromptCase(Base):
    __tablename__ = "report_prompt_cases"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False)
    case_name = Column(String(255), nullable=False)
    description = Column(Text)
    prompt_id = Column(Integer, ForeignKey("app.prompts.id", ondelete="SET NULL"), nullable=True)
    connection_id = Column(Integer, ForeignKey("app.connection_settings.id", ondelete="SET NULL"), nullable=True)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "case_name", name="uq_report_case_user"),
        {"schema": "app"}
    )


class LLMProvider(Base):
    __tablename__ = "llm_providers"
    __table_args__ = {"schema": "app"}

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(Text, nullable=False)
    provider_type = Column(Text, nullable=False)
    base_url = Column(Text, nullable=False)
    api_key = Column(Text)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    models = relationship("LLMModel", back_populates="provider")


class LLMModel(Base):
    __tablename__ = "llm_models"
    __table_args__ = {"schema": "app"}

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    provider_id = Column(UUID(as_uuid=True), ForeignKey("app.llm_providers.id", ondelete="CASCADE"), nullable=False)
    model_name = Column(Text, nullable=False)
    display_name = Column(Text)
    context_size = Column(Integer)
    max_tokens = Column(Integer)
    temperature = Column(Numeric(3, 2))
    enabled = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())
    timeout = Column(Integer, default=180)

    provider = relationship("LLMProvider", back_populates="models")


class LLMFallback(Base):
    __tablename__ = "llm_fallback"
    __table_args__ = {"schema": "app"}

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    relation_number = Column(BigInteger, nullable=False)
    model_id = Column(UUID(as_uuid=True), ForeignKey("app.llm_models.id", ondelete="CASCADE"), nullable=False)
    fallback_model_id = Column(UUID(as_uuid=True), ForeignKey("app.llm_models.id", ondelete="CASCADE"), nullable=False)
    priority = Column(Integer, default=1)

    model = relationship("LLMModel", foreign_keys=[model_id])
    fallback_model = relationship("LLMModel", foreign_keys=[fallback_model_id])


class QueryResult(Base):
    __tablename__ = "query_results"
    __table_args__ = {"schema": "app"}

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("app.users.id", ondelete="CASCADE"), nullable=False)
    sql_query = Column(Text)
    columns = Column(JSONB)
    data = Column(JSONB)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
