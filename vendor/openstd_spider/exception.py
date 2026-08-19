class OpenstdSpiderError(Exception): ...


class NotFoundError(OpenstdSpiderError):
    "资源不存在"


class DownloadError(OpenstdSpiderError):
    "下载错误"


class HandleCaptchaError(OpenstdSpiderError):
    "验证码识别错误"
