export function apiErrorMessage(error: unknown, fallback: string): string {
  const value = error as {
    message?: string
    response?: { data?: { detail?: string; error?: { message?: string } } }
  }
  return value?.response?.data?.error?.message
    || value?.response?.data?.detail
    || value?.message
    || fallback
}
