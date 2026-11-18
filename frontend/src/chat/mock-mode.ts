// One removable development switch. Production builds always use the real API.
export const mockChatEnabled = import.meta.env.DEV && new URLSearchParams(location.search).get('mock') === '1'
