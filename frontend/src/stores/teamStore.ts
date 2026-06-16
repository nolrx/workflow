/**
 * Team Store
 * Manages team state, members, and invitations
 */
import { create } from "zustand"
import { persist } from "zustand/middleware"
import { api } from "@/api/client"
import { t } from "@/i18n"

// Types
export interface Team {
  id: string
  name: string
  slug: string
  owner_id: string
  settings: Record<string, unknown>
  plan_id: string | null
  created_at: string
  updated_at: string
}

export interface TeamMember {
  id: string
  team_id: string
  user_id: string
  role: "owner" | "admin" | "member" | "viewer"
  can_create_projects: boolean
  can_use_credits: boolean
  joined_at: string
  user?: {
    id: string
    email: string
    display_name: string | null
    avatar_url: string | null
  }
}

export interface TeamInvitation {
  id: string
  team_id: string
  email: string
  role: "admin" | "member" | "viewer"
  token: string
  invited_by: string
  expires_at: string
  accepted_at: string | null
  created_at: string
}

interface CreateTeamRequest {
  name: string
}

interface InviteMemberRequest {
  email: string
  role?: "admin" | "member" | "viewer"
}

interface TeamState {
  teams: Team[]
  currentTeam: Team | null
  members: TeamMember[]
  invitations: TeamInvitation[]
  isLoading: boolean
  error: string | null

  // Actions
  fetchTeams: () => Promise<void>
  fetchTeam: (teamId: string) => Promise<void>
  createTeam: (data: CreateTeamRequest) => Promise<Team>
  setCurrentTeam: (team: Team | null) => void
  fetchMembers: (teamId: string) => Promise<void>
  inviteMember: (teamId: string, data: InviteMemberRequest) => Promise<void>
  acceptInvitation: (token: string) => Promise<void>
  clearError: () => void
}

export const useTeamStore = create<TeamState>()(
  persist(
    (set, get) => ({
      teams: [],
      currentTeam: null,
      members: [],
      invitations: [],
      isLoading: false,
      error: null,

      fetchTeams: async () => {
        set({ isLoading: true, error: null })
        try {
          const response = await api.get<{ teams: Team[] }>("/teams")
          set({ teams: response.teams, isLoading: false })

          // Set first team as current if none selected
          const { currentTeam } = get()
          if (!currentTeam && response.teams.length > 0) {
            set({ currentTeam: response.teams[0] })
          }
        } catch (error: unknown) {
          const message =
            (error as { response?: { data?: { error?: string } } })?.response
              ?.data?.error || t('errors:team.fetchTeamsFailed')
          set({ error: message, isLoading: false })
        }
      },

      fetchTeam: async (teamId) => {
        set({ isLoading: true, error: null })
        try {
          const response = await api.get<{ team: Team }>(`/teams/${teamId}`)
          set({ currentTeam: response.team, isLoading: false })
        } catch (error: unknown) {
          const message =
            (error as { response?: { data?: { error?: string } } })?.response
              ?.data?.error || t('errors:team.fetchTeamFailed')
          set({ error: message, isLoading: false })
        }
      },

      createTeam: async (data) => {
        set({ isLoading: true, error: null })
        try {
          const response = await api.post<{ team: Team; message: string }>(
            "/teams",
            data
          )
          set((state) => ({
            teams: [...state.teams, response.team],
            currentTeam: response.team,
            isLoading: false,
          }))
          return response.team
        } catch (error: unknown) {
          const message =
            (error as { response?: { data?: { error?: string } } })?.response
              ?.data?.error || t('errors:team.createTeamFailed')
          set({ error: message, isLoading: false })
          throw error
        }
      },

      setCurrentTeam: (team) => {
        set({ currentTeam: team, members: [], invitations: [] })
      },

      fetchMembers: async (teamId) => {
        set({ isLoading: true, error: null })
        try {
          const response = await api.get<{ members: TeamMember[] }>(
            `/teams/${teamId}/members`
          )
          set({ members: response.members, isLoading: false })
        } catch (error: unknown) {
          const message =
            (error as { response?: { data?: { error?: string } } })?.response
              ?.data?.error || t('errors:team.fetchMembersFailed')
          set({ error: message, isLoading: false })
        }
      },

      inviteMember: async (teamId, data) => {
        set({ isLoading: true, error: null })
        try {
          const response = await api.post<{
            invitation: TeamInvitation
            message: string
          }>(`/teams/${teamId}/invitations`, data)
          set((state) => ({
            invitations: [...state.invitations, response.invitation],
            isLoading: false,
          }))
        } catch (error: unknown) {
          const message =
            (error as { response?: { data?: { error?: string } } })?.response
              ?.data?.error || t('errors:team.inviteMemberFailed')
          set({ error: message, isLoading: false })
          throw error
        }
      },

      acceptInvitation: async (token) => {
        set({ isLoading: true, error: null })
        try {
          const response = await api.post<{ team: Team; message: string }>(
            `/teams/invitations/${token}/accept`
          )
          set((state) => ({
            teams: [...state.teams, response.team],
            isLoading: false,
          }))
        } catch (error: unknown) {
          const message =
            (error as { response?: { data?: { error?: string } } })?.response
              ?.data?.error || t('errors:team.acceptInvitationFailed')
          set({ error: message, isLoading: false })
          throw error
        }
      },

      clearError: () => set({ error: null }),
    }),
    {
      name: "team-store",
      partialize: (state) => ({
        currentTeam: state.currentTeam,
      }),
    }
  )
)
