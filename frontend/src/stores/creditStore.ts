/**
 * Credit Store
 * Manages user credits, transactions, and usage statistics
 */
import { create } from "zustand"
import { api } from "@/api/client"
import { t } from "@/i18n"

// Types
export interface CreditBalance {
  balance: number
  monthly_allocation: number
  monthly_used: number
}

export interface CreditTransaction {
  id: string
  amount: number
  balance_after: number
  transaction_type: "allocation" | "usage" | "purchase" | "refund"
  operation: string
  resource_type: string | null
  resource_id: string | null
  created_at: string
}

export interface UsageStats {
  total_credits: number
  used_credits: number
  remaining_credits: number
  usage_percentage: number
}

interface TransactionsResponse {
  transactions: CreditTransaction[]
  total: number
  page: number
  per_page: number
  has_more: boolean
}

interface CreditState {
  balance: CreditBalance | null
  transactions: CreditTransaction[]
  usageStats: UsageStats | null
  isLoading: boolean
  error: string | null

  // Pagination
  currentPage: number
  totalTransactions: number
  hasMore: boolean

  // Actions
  fetchBalance: () => Promise<void>
  refreshBalance: (teamId?: string) => Promise<void>
  fetchTransactions: (page?: number) => Promise<void>
  fetchUsageStats: () => Promise<void>
  refreshAll: () => Promise<void>
  clearError: () => void
}

export const useCreditStore = create<CreditState>((set, get) => ({
  balance: null,
  transactions: [],
  usageStats: null,
  isLoading: false,
  error: null,

  currentPage: 1,
  totalTransactions: 0,
  hasMore: false,

  fetchBalance: async () => {
    set({ isLoading: true, error: null })
    try {
      const balance = await api.get<CreditBalance>("/credits/balance")
      set({ balance, isLoading: false })
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { error?: string } } })?.response?.data
          ?.error || t('errors:credit.fetchBalanceFailed')
      set({ error: message, isLoading: false })
    }
  },

  // Lightweight balance refresh used after credit-consuming operations complete.
  // Deliberately avoids the page-level isLoading flag and stays silent on failure
  // so a background refresh never flashes a spinner or surfaces an error.
  refreshBalance: async (teamId?: string) => {
    try {
      const url = teamId
        ? `/credits/balance?team_id=${encodeURIComponent(teamId)}`
        : "/credits/balance"
      const balance = await api.get<CreditBalance>(url)
      set({ balance })
    } catch {
      // ignore — non-critical background refresh
    }
  },

  fetchTransactions: async (page = 1) => {
    set({ isLoading: true, error: null })
    try {
      const response = await api.get<TransactionsResponse>(
        `/credits/transactions?page=${page}&per_page=20`
      )

      set((state) => ({
        transactions:
          page === 1
            ? response.transactions
            : [...state.transactions, ...response.transactions],
        currentPage: response.page,
        totalTransactions: response.total,
        hasMore: response.has_more,
        isLoading: false,
      }))
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { error?: string } } })?.response?.data
          ?.error || t('errors:credit.fetchTransactionsFailed')
      set({ error: message, isLoading: false })
    }
  },

  fetchUsageStats: async () => {
    set({ isLoading: true, error: null })
    try {
      const usageStats = await api.get<UsageStats>("/credits/usage")
      set({ usageStats, isLoading: false })
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { error?: string } } })?.response?.data
          ?.error || t('errors:credit.fetchUsageStatsFailed')
      set({ error: message, isLoading: false })
    }
  },

  refreshAll: async () => {
    const { fetchBalance, fetchUsageStats, fetchTransactions } = get()
    await Promise.all([
      fetchBalance(),
      fetchUsageStats(),
      fetchTransactions(1),
    ])
  },

  clearError: () => set({ error: null }),
}))
