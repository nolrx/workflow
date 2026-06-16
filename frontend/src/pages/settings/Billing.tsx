import { useEffect } from "react"
import { CreditCard, Check, Zap, Crown } from "lucide-react"
import { useTranslation } from "react-i18next"
import { AppLayout } from "@/components/layout/AppLayout"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { useCreditStore } from "@/stores/creditStore"
import { cn } from "@/lib/utils"

interface Plan {
  id: string
  price: number
  credits: number
  popular?: boolean
  icon: typeof Zap
}

export function Billing() {
  const { t } = useTranslation("settings")
  const { balance, usageStats, fetchBalance, fetchUsageStats } = useCreditStore()

  const plans: Plan[] = [
    {
      id: "free",
      price: 0,
      credits: 30,
      icon: Zap,
    },
    {
      id: "pro",
      price: 49,
      credits: 300,
      popular: true,
      icon: Crown,
    },
    {
      id: "team",
      price: 149,
      credits: 1000,
      icon: CreditCard,
    },
  ]

  useEffect(() => {
    fetchBalance()
    fetchUsageStats()
  }, [fetchBalance, fetchUsageStats])

  return (
    <AppLayout title={t("billing.title")}>
      <div className="space-y-8">
        <div>
          <h1 className="text-2xl font-semibold">{t("billing.title")}</h1>
          <p className="text-muted-foreground">
            {t("billing.subtitle")}
          </p>
        </div>

        {/* Current Usage */}
        <Card>
          <CardHeader>
            <CardTitle>{t("billing.usage.title")}</CardTitle>
            <CardDescription>
              {t("billing.usage.description")}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid gap-4 md:grid-cols-3">
              <div className="text-center">
                <p className="text-3xl font-bold">
                  {balance?.balance || 0}
                </p>
                <p className="text-sm text-muted-foreground">
                  {t("billing.usage.remaining")}
                </p>
              </div>
              <div className="text-center">
                <p className="text-3xl font-bold">
                  {balance?.monthly_used || 0}
                </p>
                <p className="text-sm text-muted-foreground">
                  {t("billing.usage.used")}
                </p>
              </div>
              <div className="text-center">
                <p className="text-3xl font-bold">
                  {usageStats?.usage_percentage || 0}%
                </p>
                <p className="text-sm text-muted-foreground">
                  {t("billing.usage.usage")}
                </p>
              </div>
            </div>
            <div className="mt-4">
              <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full bg-primary transition-all"
                  style={{ width: `${usageStats?.usage_percentage || 0}%` }}
                />
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Plans */}
        <div>
          <h2 className="text-xl font-semibold mb-4">{t("billing.plans.title")}</h2>
          <div className="grid gap-6 md:grid-cols-3">
            {plans.map((plan) => (
              <Card
                key={plan.id}
                className={cn(
                  "relative",
                  plan.popular && "border-primary shadow-md"
                )}
              >
                {plan.popular && (
                  <div className="absolute -top-3 left-1/2 -translate-x-1/2">
                    <span className="rounded-full bg-primary px-3 py-1 text-xs font-medium text-primary-foreground">
                      {t("billing.plans.popular")}
                    </span>
                  </div>
                )}
                <CardHeader>
                  <div className="flex items-center gap-2">
                    <plan.icon className="h-5 w-5" />
                    <CardTitle>{t(`billing.plans.${plan.id}.name`)}</CardTitle>
                  </div>
                  <CardDescription>
                    <span className="text-3xl font-bold text-foreground">
                      ¥{plan.price}
                    </span>
                    {plan.price > 0 && (
                      <span className="text-muted-foreground">{t("billing.plans.monthly")}</span>
                    )}
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="text-sm text-muted-foreground">
                    {plan.credits} credits per month
                  </div>
                  <ul className="space-y-2">
                    {(t(`billing.plans.${plan.id}.features`, { returnObjects: true }) as string[]).map((feature, i) => (
                      <li key={i} className="flex items-center gap-2 text-sm">
                        <Check className="h-4 w-4 text-primary" />
                        {feature}
                      </li>
                    ))}
                  </ul>
                  <Button
                    className="w-full"
                    variant={plan.popular ? "default" : "outline"}
                  >
                    {plan.id === "free" ? t("billing.plans.currentPlan") : t("billing.plans.upgrade")}
                  </Button>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>

        {/* Payment History */}
        <Card>
          <CardHeader>
            <CardTitle>{t("billing.paymentHistory.title")}</CardTitle>
            <CardDescription>
              {t("billing.paymentHistory.description")}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="text-center py-8 text-muted-foreground">
              {t("billing.paymentHistory.empty")}
            </div>
          </CardContent>
        </Card>
      </div>
    </AppLayout>
  )
}
