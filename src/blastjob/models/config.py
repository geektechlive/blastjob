from pydantic import BaseModel, Field


class PathsConfig(BaseModel):
    data_dir: str = "~/.blastjob"
    output_dir: str = "~/Documents/blastjob"


class LLMConfig(BaseModel):
    # "auto" checks ANTHROPIC_API_KEY, then OPENAI_API_KEY, then claude-cli
    provider: str = "auto"

    # Anthropic settings
    anthropic_api_key_env: str = "ANTHROPIC_API_KEY"
    anthropic_model: str = "claude-sonnet-4-6"

    # OpenAI settings
    openai_api_key_env: str = "OPENAI_API_KEY"
    openai_model: str = "gpt-4o"


class GenerationConfig(BaseModel):
    max_web_searches: int = 3
    stream_to_tui: bool = True


class PricingConfig(BaseModel):
    input_per_mtok: float = 3.00
    output_per_mtok: float = 15.00
    cache_write_per_mtok: float = 3.75
    cache_read_per_mtok: float = 0.30


class BlastJobConfig(BaseModel):
    model_config = {"extra": "ignore"}

    paths: PathsConfig = Field(default_factory=PathsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)

    # Back-compat alias so old code still works during migration
    @property
    def anthropic(self) -> "LLMConfig":
        return self.llm
