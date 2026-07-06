import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Info } from "lucide-react";
import { toast } from "sonner";
import { AgentRunPanel } from "@/components/agent/AgentRunPanel";
import { CodeStepper } from "@/components/code/CodeStepper";
import { ConversationRail } from "@/components/code/ConversationRail";
import { deriveStageNav, type DisplayStage } from "@/components/code/stages";
// Figma「关联设计 / HTML 导出到 Figma」暂时下线(先停止显示),保留 import 以便后续恢复
// import { FigmaExportDialog } from "@/components/code/FigmaExportDialog";
// import { FigmaImportDialog } from "@/components/code/FigmaImportDialog";
import { GitHubRepoCard } from "@/components/code/GitHubRepoCard";
import { PreviewThumbnailPanel } from "@/components/code/PreviewThumbnailPanel";
import { AppLayout } from "@/components/layout/AppLayout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useAgentStore } from "@/stores/agentStore";
import { useCodeStore } from "@/stores/codeStore";

/**
 * Windowed conversational Code workspace: the stepper header is the navigation
 * bar and each stage is its own conversation window — the rail below shows only
 * the selected stage's transcript, artifact card and contextual composer, so the
 * user stays focused on one step at a time. The view auto-follows the live
 * position (a new review gate, a failure) but the user can click any reached
 * stage to look back; work waiting elsewhere is one tap away. The build is still
 * driven through chat and pauses after each reviewed document for approval.
 */
export function CodeStudio() {
    const { t } = useTranslation("code");
    const { t: ta } = useTranslation("agent");
    // const { t: tc } = useTranslation("canvas");  // 画布模式入口已下线(随之停用)

    const project = useCodeStore((state) => state.project);
    const selectedStyleIds = useCodeStore((state) => state.selectedStyleIds);
    const activeAction = useCodeStore((state) => state.activeAction);
    const error = useCodeStore((state) => state.error);
    const fetchStyles = useCodeStore((state) => state.fetchStyles);
    const loadProject = useCodeStore((state) => state.loadProject);
    const clearError = useCodeStore((state) => state.clearError);

    const startAgentRun = useAgentStore((state) => state.startRun);
    const resumeRun = useAgentStore((state) => state.resumeRun);
    const openLatestRunForResource = useAgentStore(
        (state) => state.openLatestRunForResource,
    );
    const resetAgentRun = useAgentStore((state) => state.reset);
    const openPanel = useAgentStore((state) => state.openPanel);
    const agentRun = useAgentStore((state) => state.run);
    const events = useAgentStore((state) => state.events);

    const [requirementInput, setRequirementInput] = useState("");
    // Which stage window is shown. It auto-follows the live position (see below),
    // but the user can navigate freely via the stepper.
    const [viewStage, setViewStage] = useState<DisplayStage>("requirements");

    const { projectId } = useParams<{ projectId?: string }>();
    const navigate = useNavigate();
    // Which session's agent run is currently bound to the workspace. Keyed by a ref
    // (not a reactive guard on run.resource_id) because openRun briefly nulls the
    // run mid-load, which would otherwise re-fire the replay.
    const boundRunResourceRef = useRef<string | null>(null);

    useEffect(() => {
        fetchStyles();
    }, [fetchStyles]);

    // Deep-link / session switch: when opened at /code/:projectId, load that project.
    useEffect(() => {
        if (projectId && project?.id !== projectId) {
            void loadProject(projectId);
        }
    }, [projectId, project?.id, loadProject]);

    // Blank-studio auto-switch: when a fresh run creates a project while we are on
    // /code (no projectId), promote the URL to /code/:projectId so the sidebar
    // updates and the user gets a shareable project page instead of the generic
    // studio route. Only auto-switch for in-flight runs so completed/historical
    // runs opened at /code stay there for review.
    const autoNavigatedForRunRef = useRef<string | null>(null);
    useEffect(() => {
        const rid = agentRun?.resource_id;
        const status = agentRun?.status;
        if (!rid || projectId) return;
        if (autoNavigatedForRunRef.current === rid) return;
        if (status !== "running" && status !== "queued") return;
        autoNavigatedForRunRef.current = rid;
        navigate(`/code/${rid}`);
    }, [agentRun?.resource_id, agentRun?.status, projectId, navigate]);

    // ...and replay that session's latest agent run. The whole transcript (and the
    // inline document cards rendered inside it) is event-sourced off the run, so
    // without this a historical session would open to an empty conversation even
    // though loadProject already fetched its document content. On a blank studio
    // clear the ref so reopening a session reprocesses it; when already bound to
    // this session's live run (e.g. just started inline) keep the live run.
    useEffect(() => {
        if (!projectId) {
            boundRunResourceRef.current = null;
            return;
        }
        if (boundRunResourceRef.current === projectId) return;
        if (agentRun?.resource_id === projectId) {
            boundRunResourceRef.current = projectId;
            return;
        }
        boundRunResourceRef.current = projectId;
        // Bind to the conversation/document run specifically. A project's resource_id
        // also collects auxiliary runs (frontend build, figma slice, canvas) whose
        // events carry no review gates, so replaying "the latest run of any kind"
        // would rebind the transcript to one with no document cards — the reopened
        // session would look empty. The app preview resolves its own frontend run.
        void openLatestRunForResource(projectId, "code_full_generation").then(
            (found) => {
                // Legacy / run-less project: drop any prior session's run so its transcript
                // doesn't bleed through onto this one.
                if (!found && boundRunResourceRef.current === projectId)
                    resetAgentRun();
            },
        );
    }, [
        projectId,
        agentRun?.resource_id,
        openLatestRunForResource,
        resetAgentRun,
    ]);

    useEffect(() => {
        if (!error) return;
        toast.error(error);
        clearError();
    }, [error, clearError]);

    // Load the project's editable content into the cards whenever the run produces
    // or revises a document (a new awaiting-review event arrives) or settles — so
    // the inline artifacts always reflect the latest generated content.
    const reviewTick = events.filter(
        (event) => event.event_type === "step_awaiting_review",
    ).length;
    useEffect(() => {
        const rid = agentRun?.resource_id;
        const status = agentRun?.status;
        if (
            rid &&
            (status === "paused" ||
                status === "completed" ||
                status === "partial")
        ) {
            void loadProject(rid);
        }
    }, [agentRun?.resource_id, agentRun?.status, reviewTick, loadProject]);

    // Auto-follow the live position: when the run advances to a new review gate,
    // fails, or completes, snap the visible window to it. Tracked via a ref so the
    // snap only happens when the position actually changes — manual navigation to
    // an earlier window within the same position sticks (mirrors the per-card
    // auto-focus pattern in the rail).
    const focusStage = deriveStageNav(agentRun).focusStage;
    const prevFocusRef = useRef<DisplayStage | null>(null);
    useEffect(() => {
        if (focusStage !== prevFocusRef.current) {
            setViewStage(focusStage);
            prevFocusRef.current = focusStage;
        }
    }, [focusStage]);

    const handleStart = async () => {
        const requirement = requirementInput.trim();
        if (!requirement) {
            toast.error(ta("swarm.requirementRequired"));
            return;
        }
        try {
            await startAgentRun({
                domain: "code",
                workflow: "code_full_generation",
                resource_type: project ? "code_project" : undefined,
                resource_id: project?.id,
                config: {
                    requirement,
                    title: project?.title,
                    style_ids: selectedStyleIds,
                },
            });
        } catch (err) {
            const message =
                (err as { response?: { data?: { message?: string } } })
                    ?.response?.data?.message || ta("toast.startFailed");
            toast.error(message);
        }
    };

    const handleApprove = async () => {
        try {
            await resumeRun("approve");
        } catch {
            toast.error(ta("toast.startFailed"));
        }
    };

    const handleRevise = async (instruction: string) => {
        try {
            await resumeRun("revise", instruction);
        } catch {
            toast.error(ta("toast.startFailed"));
        }
    };

    const setNewProjectDialogOpen = useCodeStore(
        (state) => state.setNewProjectDialogOpen,
    );

    const handleNewProject = () => {
        // Go through setCurrentProject (not a raw setState) so the store's load-sequence
        // guard is bumped: any project fetch still in flight for the old session is then
        // dropped instead of repopulating this freshly-cleared session.
        useCodeStore.getState().setCurrentProject(null);
        boundRunResourceRef.current = null;
        resetAgentRun();
        setRequirementInput("");
        setViewStage("requirements");
        // Drop the :projectId from the URL so the deep-link effect does not reload
        // the previous session and the next run starts a fresh project.
        navigate("/code");
        setNewProjectDialogOpen(true);
    };

    // The preview thumbnails live in a right-hand rail that slides in once images
    // exist (or are being generated). Scoped to the style / app windows so they
    // reinforce the focus of those steps rather than following the user everywhere.
    const showThumbnails =
        ((project?.preview_images?.length ?? 0) > 0 ||
            activeAction === "preview") &&
        (viewStage === "style" || viewStage === "app");

    // Figma belongs to the UI-generation stage: only surface it once the project
    // has reached the preview / UI-baseline stage, so it never clutters or
    // interrupts the document stages (requirements / flow / documents / style).
    // 注:Figma「关联设计 / HTML 导出到 Figma」暂时停止显示,下方 header 中相关
    // 渲染已注释,故此处的 inUiStage 暂时不再被使用,后续恢复时一并取消注释。
    // const inUiStage =
    //     !!project && ["preview_ready", "ui_confirmed"].includes(project.status);

    return (
        <AppLayout title={t("title")}>
            <AgentRunPanel />
            <div
                className={`mx-auto flex h-full min-h-0 w-full flex-col gap-3 transition-[max-width] duration-300 ${
                    showThumbnails ? "max-w-6xl" : "max-w-4xl"
                }`}
            >
                {/* Stage progress header */}
                <div className="flex flex-col gap-2 rounded-xl border bg-card px-3 py-2.5 sm:flex-row sm:items-center sm:gap-3 sm:px-4 sm:py-3">
                    <div className="min-w-0 flex-1">
                        <CodeStepper
                            viewStage={viewStage}
                            onSelect={setViewStage}
                        />
                    </div>
                    <div className="flex shrink-0 items-center justify-end gap-2">
                        {/* Blueprint / canvas mode entry —— 暂时停止显示(先下线),保留代码以便后续恢复:
                        {project && (
                            <Button variant="outline" size="sm" className="h-8 sm:h-9"
                                onClick={() => navigate(`/code/${project.id}/canvas`)}>
                                <Workflow className="h-4 w-4 sm:mr-1.5" />
                                <span className="hidden sm:inline">{tc("entry")}</span>
                            </Button>
                        )} */}
                        {/* Dev Mode entry —— 已移除:开发模式入口现统一由「生成项目/重新生成项目」
                            按钮承载(CodeAppPreview → /code/:id/dev?start=plan),不再在流程步骤条上
                            单列入口。保留代码以便需要时恢复:
                        {project && (
                            <Button
                                variant="outline"
                                size="sm"
                                className="h-8 sm:h-9"
                                onClick={() => navigate(`/code/${project.id}/dev`)}
                            >
                                <Terminal className="h-4 w-4 sm:mr-1.5" />
                                <span className="hidden sm:inline">{t("dev.entry")}</span>
                            </Button>
                        )} */}
                        {/* GitHub auto-sync status (read-only): repo link + latest push. Renders
                nothing until the session has a repo or a sync event. */}
                        {project && <GitHubRepoCard projectId={project.id} />}
                        {/* Figma 关联设计 / HTML 导出到 Figma —— 暂时停止显示(先下线),
                保留代码以便后续恢复:
                {inUiStage && (
                    <FigmaImportDialog projectId={project!.id} />
                )}
                {inUiStage && (
                    <FigmaExportDialog
                        projectId={project!.id}
                        source="html"
                        triggerLabel={t("figma.exportHtml")}
                    />
                )} */}
                        {agentRun && (
                            <>
                                <Badge
                                    variant={
                                        agentRun.status === "paused"
                                            ? "default"
                                            : "outline"
                                    }
                                >
                                    {ta(`status.${agentRun.status}`, {
                                        defaultValue: agentRun.status,
                                    })}
                                </Badge>
                                <Button
                                    variant="ghost"
                                    size="sm"
                                    className="h-8 w-8 px-0 sm:h-9 sm:w-auto sm:px-3"
                                    onClick={openPanel}
                                >
                                    <Info className="h-4 w-4 sm:hidden" />
                                    <span className="hidden sm:inline">
                                        {ta("panel.viewDetail")}
                                    </span>
                                </Button>
                            </>
                        )}
                    </div>
                </div>

                {/* Two-column: left conversation, right preview thumbnails (slides in). */}
                <div className="flex min-h-0 flex-1 flex-col gap-3 lg:flex-row">
                    <Card className="flex min-h-0 flex-1 flex-col overflow-hidden p-0">
                        <ConversationRail
                            viewStage={viewStage}
                            onSelectStage={setViewStage}
                            requirementDraft={requirementInput}
                            onRequirementChange={setRequirementInput}
                            onStart={handleStart}
                            onApprove={handleApprove}
                            onRevise={handleRevise}
                            onNewProject={handleNewProject}
                        />
                    </Card>
                    {showThumbnails && (
                        <div className="min-h-0 max-h-[45vh] shrink-0 duration-300 animate-in fade-in slide-in-from-right-8 lg:h-full lg:max-h-none lg:w-[22rem]">
                            <PreviewThumbnailPanel />
                        </div>
                    )}
                </div>
            </div>
        </AppLayout>
    );
}

export default CodeStudio;
