/**
 * PPT Studio Home Page
 * Entry point for creating new PPT projects
 */
import { useState } from "react"
import { useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { usePPTStore } from "@/stores/pptStore"
import { AppLayout } from "@/components/layout/AppLayout"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Textarea } from "@/components/ui/textarea"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Loader2, Lightbulb, FileText, ListOrdered } from "lucide-react"

type CreationType = "idea" | "outline" | "descriptions"

export default function PPTHome() {
  const navigate = useNavigate()
  const { t } = useTranslation('ppt')
  const { createProject, isLoading, error } = usePPTStore()
  const [creationType, setCreationType] = useState<CreationType>("idea")
  const [ideaPrompt, setIdeaPrompt] = useState("")
  const [outlineText, setOutlineText] = useState("")
  const [descriptionText, setDescriptionText] = useState("")

  const handleCreate = async () => {
    try {
      const project = await createProject({
        creation_type: creationType,
        idea_prompt: creationType === "idea" ? ideaPrompt : undefined,
        outline_text: creationType === "outline" ? outlineText : undefined,
        description_text:
          creationType === "descriptions" ? descriptionText : undefined,
      })
      navigate(`/ppt/project/${project.id}`)
    } catch (err) {
      console.error("Failed to create project:", err)
    }
  }

  const getPlaceholder = () => {
    switch (creationType) {
      case "idea":
        return t('home.ideaTab.placeholder')
      case "outline":
        return t('home.outlineTab.placeholder')
      case "descriptions":
        return t('home.descriptionsTab.placeholder')
      default:
        return ""
    }
  }

  const getCurrentValue = () => {
    switch (creationType) {
      case "idea":
        return ideaPrompt
      case "outline":
        return outlineText
      case "descriptions":
        return descriptionText
      default:
        return ""
    }
  }

  const isValid = getCurrentValue().trim().length > 0

  return (
    <AppLayout>
      <div className="container mx-auto max-w-4xl py-8">
        <div className="mb-8 text-center">
          <h1 className="text-3xl font-bold mb-2">{t('home.title')}</h1>
          <p className="text-muted-foreground">
            {t('home.subtitle')}
          </p>
        </div>

        <Card>
        <CardHeader>
          <CardTitle>{t('home.createNew')}</CardTitle>
        </CardHeader>
        <CardContent>
          <Tabs
            value={creationType}
            onValueChange={(v: string) => setCreationType(v as CreationType)}
          >
            <TabsList className="grid w-full grid-cols-3 mb-6">
              <TabsTrigger value="idea" className="flex items-center gap-2">
                <Lightbulb className="h-4 w-4" />
                {t('home.tabs.idea')}
              </TabsTrigger>
              <TabsTrigger value="outline" className="flex items-center gap-2">
                <ListOrdered className="h-4 w-4" />
                {t('home.tabs.outline')}
              </TabsTrigger>
              <TabsTrigger
                value="descriptions"
                className="flex items-center gap-2"
              >
                <FileText className="h-4 w-4" />
                {t('home.tabs.descriptions')}
              </TabsTrigger>
            </TabsList>

            <TabsContent value="idea">
              <div className="space-y-4">
                <p className="text-sm text-muted-foreground">
                  {t('home.ideaTab.description')}
                </p>
                <Textarea
                  placeholder={getPlaceholder()}
                  value={ideaPrompt}
                  onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setIdeaPrompt(e.target.value)}
                  rows={6}
                  className="resize-none"
                />
              </div>
            </TabsContent>

            <TabsContent value="outline">
              <div className="space-y-4">
                <p className="text-sm text-muted-foreground">
                  {t('home.outlineTab.description')}
                </p>
                <Textarea
                  placeholder={getPlaceholder()}
                  value={outlineText}
                  onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setOutlineText(e.target.value)}
                  rows={8}
                  className="resize-none font-mono text-sm"
                />
              </div>
            </TabsContent>

            <TabsContent value="descriptions">
              <div className="space-y-4">
                <p className="text-sm text-muted-foreground">
                  {t('home.descriptionsTab.description')}
                </p>
                <Textarea
                  placeholder={getPlaceholder()}
                  value={descriptionText}
                  onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setDescriptionText(e.target.value)}
                  rows={10}
                  className="resize-none font-mono text-sm"
                />
              </div>
            </TabsContent>
          </Tabs>

          {error && (
            <div className="mt-4 p-3 bg-destructive/10 text-destructive rounded-md text-sm">
              {error}
            </div>
          )}

          <div className="mt-6 flex justify-end gap-3">
            <Button variant="outline" onClick={() => navigate("/ppt/history")}>
              {t('history.title')}
            </Button>
            <Button onClick={handleCreate} disabled={!isValid || isLoading}>
              {isLoading ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  {t('home.creating')}
                </>
              ) : (
                t('home.createProject')
              )}
            </Button>
          </div>
        </CardContent>
      </Card>
      </div>
    </AppLayout>
  )
}
