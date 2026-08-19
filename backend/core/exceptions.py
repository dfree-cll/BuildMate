"""统一异常体系"""
class BuildMateBaseError(Exception):
    def __init__(self, message: str, agent_type: str = "", details: dict | None = None):
        super().__init__(message)
        self.agent_type = agent_type
        self.details = details or {}


class LLMAPIError(BuildMateBaseError):        # 可重试
    pass

class AgentExecutionError(BuildMateBaseError):
    pass

class PipelineError(BuildMateBaseError):
    pass

class IntentRouteError(BuildMateBaseError):
    pass

class FileParseError(BuildMateBaseError):
    pass

class InvalidInputError(BuildMateBaseError):   # 不可重试
    pass

class AuthenticationError(BuildMateBaseError):  # 不可重试
    pass
