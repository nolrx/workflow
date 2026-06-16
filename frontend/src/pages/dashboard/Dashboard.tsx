import { Link } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { Presentation, Image, ArrowRight } from "lucide-react"
import { AppLayout } from "@/components/layout/AppLayout"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"

export function Dashboard() {
  const { t } = useTranslation('dashboard')
  const { t: tc } = useTranslation('common')

  return (
    <AppLayout title={tc('nav.dashboard')}>
      <div className="space-y-8">
        {/* Welcome Section */}
        <div>
          <h2 className="text-2xl font-bold tracking-tight">{t('welcome.title')}</h2>
          <p className="text-muted-foreground">
            {t('welcome.subtitle')}
          </p>
        </div>

        {/* Quick Actions */}
        <div className="grid gap-6 md:grid-cols-2">
          <Card>
            <CardHeader>
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
                  <Presentation className="h-5 w-5 text-primary" />
                </div>
                <div>
                  <CardTitle>{t('cards.ppt.title')}</CardTitle>
                  <CardDescription>{t('cards.ppt.subtitle')}</CardDescription>
                </div>
              </div>
            </CardHeader>
            <CardContent>
              <p className="mb-4 text-sm text-muted-foreground">
                {t('cards.ppt.description')}
              </p>
              <Button asChild>
                <Link to="/ppt/create">
                  {t('cards.ppt.button')}
                  <ArrowRight className="ml-2 h-4 w-4" />
                </Link>
              </Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
                  <Image className="h-5 w-5 text-primary" />
                </div>
                <div>
                  <CardTitle>{t('cards.redbook.title')}</CardTitle>
                  <CardDescription>{t('cards.redbook.subtitle')}</CardDescription>
                </div>
              </div>
            </CardHeader>
            <CardContent>
              <p className="mb-4 text-sm text-muted-foreground">
                {t('cards.redbook.description')}
              </p>
              <Button asChild>
                <Link to="/redink/create">
                  {t('cards.redbook.button')}
                  <ArrowRight className="ml-2 h-4 w-4" />
                </Link>
              </Button>
            </CardContent>
          </Card>
        </div>

        {/* Recent Projects */}
        <div>
          <h3 className="mb-4 text-lg font-semibold">{t('recentProjects.title')}</h3>
          <Card>
            <CardContent className="flex items-center justify-center py-12">
              <p className="text-sm text-muted-foreground">
                {t('recentProjects.empty')}
              </p>
            </CardContent>
          </Card>
        </div>
      </div>
    </AppLayout>
  )
}
