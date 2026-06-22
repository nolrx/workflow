import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import { figmaApi, type FigmaCredential } from "@/api/figma"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

/**
 * Manage the user's Figma personal access token (PAT).
 *
 * Self-contained: loads the stored credential on mount, lets the user paste a
 * token (validated + stored encrypted server-side) or disconnect. Never sees the
 * token after saving — only a masked `****last4`. Notifies the parent of the
 * connected state so the import flow can gate on it.
 */
export function FigmaCredentialCard({
  onConnectedChange,
}: {
  onConnectedChange?: (connected: boolean) => void
}) {
  const { t } = useTranslation("code")
  const [credential, setCredential] = useState<FigmaCredential | null>(null)
  const [token, setToken] = useState("")
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    let active = true
    figmaApi
      .getCredential()
      .then((cred) => {
        if (!active) return
        setCredential(cred)
        onConnectedChange?.(cred.has_token)
      })
      .catch(() => {
        /* non-critical: treat as not connected */
      })
    return () => {
      active = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleSave = async () => {
    const value = token.trim()
    if (!value) return
    setLoading(true)
    try {
      const cred = await figmaApi.saveCredential(value)
      setCredential(cred)
      setToken("")
      onConnectedChange?.(true)
      toast.success(t("figma.connected"))
    } catch (error) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        t("figma.connectFailed")
      toast.error(message)
    } finally {
      setLoading(false)
    }
  }

  const handleDisconnect = async () => {
    setLoading(true)
    try {
      await figmaApi.deleteCredential()
      setCredential({ has_token: false })
      onConnectedChange?.(false)
      toast.success(t("figma.disconnected"))
    } catch {
      toast.error(t("figma.connectFailed"))
    } finally {
      setLoading(false)
    }
  }

  if (credential?.has_token) {
    return (
      <div className="flex items-center justify-between rounded-md border bg-muted/30 px-3 py-2">
        <div className="min-w-0 text-sm">
          <span className="font-medium">{t("figma.connectedLabel")}</span>
          <span className="ml-2 text-muted-foreground">****{credential.token_last4}</span>
        </div>
        <Button variant="ghost" size="sm" onClick={handleDisconnect} disabled={loading}>
          {t("figma.disconnect")}
        </Button>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <Label htmlFor="figma-token">{t("figma.tokenLabel")}</Label>
      <Input
        id="figma-token"
        type="password"
        value={token}
        placeholder={t("figma.tokenPlaceholder")}
        onChange={(event) => setToken(event.target.value)}
        autoComplete="off"
      />
      <div className="flex items-center justify-between gap-2">
        <a
          href="https://www.figma.com/developers/api#access-tokens"
          target="_blank"
          rel="noreferrer"
          className="text-xs text-muted-foreground underline-offset-2 hover:underline"
        >
          {t("figma.tokenHelp")}
        </a>
        <Button size="sm" onClick={handleSave} disabled={loading || !token.trim()}>
          {t("figma.connect")}
        </Button>
      </div>
    </div>
  )
}

export default FigmaCredentialCard
