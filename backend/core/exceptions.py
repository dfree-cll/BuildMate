"""统一异常体系"""
class BuildMateBaseError(Exception):
    def __init__(self, message: str, agent_type: str = "", details: dict | None = None):
        super().__init__(message)
        self.agent_type = agent_type
        self.details = details or {}


class LLMAPIError(BuildMateBaseError):        # 可重试
    pass

class InvalidInputError(BuildMateBaseError):   # 不可重试
    pass

class AuthenticationError(BuildMateBaseError):  # 不可重试
    pass


# ── MCP 工具网关异常（继承对应基类以复用 retry.py 的重试/不重试分类）──
class MCPError(BuildMateBaseError):
    """MCP 工具网关错误基类"""

class MCPToolTimeout(MCPError, TimeoutError):        # 可重试
    pass

class MCPNetworkError(MCPError, ConnectionError):    # 可重试
    pass

class MCPToolError(MCPError):                        # 工具服务端返回的业务错误
    pass

class MCPToolDenied(MCPError, AuthenticationError):  # 工具访问被拒（ACL）：不可重试
    pass

class MCPInvalidParams(MCPError, InvalidInputError): # 参数校验失败：不可重试
    pass
