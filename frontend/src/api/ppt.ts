/**
 * PPT API Endpoints
 * API functions for PPT module - materials, reference files, templates, settings
 */
import { apiClient, api } from "./client"

// ===== Types =====

export interface Material {
  id: string
  user_id: string
  project_id: string | null
  filename: string
  url: string
  relative_path: string
  name?: string
  prompt?: string
  original_filename?: string
  source_filename?: string
  created_at: string
  updated_at: string
}

export interface ReferenceFile {
  id: string
  user_id: string
  project_id: string | null
  filename: string
  file_size: number
  file_type: string
  parse_status: "pending" | "parsing" | "completed" | "failed"
  markdown_content: string | null
  error_message: string | null
  image_caption_failed_count?: number
  created_at: string
  updated_at: string
}

export interface UserTemplate {
  template_id: string
  user_id: string
  team_id: string | null
  name: string | null
  template_image_url: string
  visibility: "private" | "team" | "public"
  created_at: string
  updated_at: string
}

export interface PPTSettings {
  id: string
  user_id: string
  ai_provider: string | null
  api_base_url: string | null
  has_api_key: boolean
  text_model: string | null
  image_model: string | null
  image_resolution: string
  image_aspect_ratio: string
  max_description_workers: number
  max_image_workers: number
  output_language: "zh" | "en" | "ja" | "auto"
  created_at: string
  updated_at: string
}

export interface ImageVersion {
  id: string
  page_id: string
  version_number: number
  image_path: string
  image_url: string
  is_current: boolean
  created_at: string
}

export type OutputLanguage = "zh" | "en" | "ja" | "auto"

// API Response wrapper
interface ApiResponse<T> {
  success: boolean
  data: T
  message?: string
}

// ===== Material APIs =====

/**
 * List materials for a project
 */
export const listProjectMaterials = async (
  projectId: string
): Promise<{ materials: Material[]; count: number }> => {
  const response = await api.get<ApiResponse<{ materials: Material[]; count: number }>>(
    `/ppt/projects/${projectId}/materials`
  )
  return response.data
}

/**
 * List all user materials
 * @param projectId - Filter: 'all' | 'global' | specific project ID
 */
export const listMaterials = async (
  projectId?: string
): Promise<{ materials: Material[]; count: number }> => {
  let url = "/ppt/materials"
  if (projectId && projectId !== "all") {
    url += `?project_id=${projectId}`
  }
  const response = await api.get<ApiResponse<{ materials: Material[]; count: number }>>(url)
  return response.data
}

/**
 * Upload material to a project
 */
export const uploadProjectMaterial = async (
  projectId: string,
  file: File
): Promise<Material> => {
  const formData = new FormData()
  formData.append("file", file)

  const response = await apiClient.post<ApiResponse<Material>>(
    `/ppt/projects/${projectId}/materials/upload`,
    formData
  )
  return response.data.data
}

/**
 * Upload global material (not bound to any project)
 */
export const uploadGlobalMaterial = async (file: File): Promise<Material> => {
  const formData = new FormData()
  formData.append("file", file)

  const response = await apiClient.post<ApiResponse<Material>>(
    "/ppt/materials/upload",
    formData
  )
  return response.data.data
}

/**
 * Upload material (auto-detect project or global)
 */
export const uploadMaterial = async (
  file: File,
  projectId?: string | null
): Promise<Material> => {
  if (projectId && projectId !== "none" && projectId !== "global") {
    return uploadProjectMaterial(projectId, file)
  }
  return uploadGlobalMaterial(file)
}

/**
 * Delete a material
 */
export const deleteMaterial = async (materialId: string): Promise<void> => {
  await api.delete(`/ppt/materials/${materialId}`)
}

/**
 * Associate materials to a project by URLs
 */
export const associateMaterials = async (
  projectId: string,
  materialUrls: string[]
): Promise<{ updated_ids: string[]; count: number }> => {
  const response = await api.post<ApiResponse<{ updated_ids: string[]; count: number }>>(
    "/ppt/materials/associate",
    { project_id: projectId, material_urls: materialUrls }
  )
  return response.data
}

// ===== Reference File APIs =====

/**
 * Upload a reference file
 */
export const uploadReferenceFile = async (
  file: File,
  projectId?: string | null
): Promise<{ file: ReferenceFile }> => {
  const formData = new FormData()
  formData.append("file", file)
  if (projectId && projectId !== "none") {
    formData.append("project_id", projectId)
  }

  const response = await apiClient.post<ApiResponse<{ file: ReferenceFile }>>(
    "/ppt/reference-files/upload",
    formData
  )
  return response.data.data
}

/**
 * Get reference file details
 */
export const getReferenceFile = async (
  fileId: string
): Promise<{ file: ReferenceFile }> => {
  const response = await api.get<ApiResponse<{ file: ReferenceFile }>>(
    `/ppt/reference-files/${fileId}`
  )
  return response.data
}

/**
 * List reference files for a project
 * @param projectId - 'all' | 'global' | 'none' | specific project ID
 */
export const listReferenceFiles = async (
  projectId: string
): Promise<{ files: ReferenceFile[] }> => {
  const response = await api.get<ApiResponse<{ files: ReferenceFile[] }>>(
    `/ppt/reference-files/project/${projectId}`
  )
  return response.data
}

/**
 * Delete a reference file
 */
export const deleteReferenceFile = async (fileId: string): Promise<void> => {
  await api.delete(`/ppt/reference-files/${fileId}`)
}

/**
 * Trigger file parsing
 */
export const triggerFileParse = async (
  fileId: string
): Promise<{ file: ReferenceFile; message: string }> => {
  const response = await api.post<ApiResponse<{ file: ReferenceFile; message: string }>>(
    `/ppt/reference-files/${fileId}/parse`
  )
  return response.data
}

/**
 * Parse reference file (alias for triggerFileParse)
 */
export const parseReferenceFile = triggerFileParse

/**
 * Associate file to project
 */
export const associateFileToProject = async (
  fileId: string,
  projectId: string
): Promise<{ file: ReferenceFile }> => {
  const response = await api.post<ApiResponse<{ file: ReferenceFile }>>(
    `/ppt/reference-files/${fileId}/associate`,
    { project_id: projectId }
  )
  return response.data
}

/**
 * Dissociate file from project
 */
export const dissociateFileFromProject = async (
  fileId: string
): Promise<{ file: ReferenceFile; message: string }> => {
  const response = await api.post<ApiResponse<{ file: ReferenceFile; message: string }>>(
    `/ppt/reference-files/${fileId}/dissociate`
  )
  return response.data
}

/**
 * Download reference file
 */
export const downloadReferenceFile = async (fileId: string): Promise<Blob> => {
  const response = await apiClient.get(`/ppt/reference-files/${fileId}/download`, {
    responseType: "blob",
  })
  return response.data
}

// ===== User Template APIs =====

/**
 * Upload a user template
 */
export const uploadUserTemplate = async (
  file: File,
  name?: string,
  visibility?: "private" | "team" | "public"
): Promise<UserTemplate> => {
  const formData = new FormData()
  formData.append("template_image", file)
  if (name) {
    formData.append("name", name)
  }
  if (visibility) {
    formData.append("visibility", visibility)
  }

  const response = await apiClient.post<ApiResponse<UserTemplate>>(
    "/ppt/user-templates",
    formData
  )
  return response.data.data
}

/**
 * List user templates
 */
export const listUserTemplates = async (
  visibility?: string,
  teamId?: string
): Promise<{ templates: UserTemplate[] }> => {
  const params = new URLSearchParams()
  if (visibility) params.append("visibility", visibility)
  if (teamId) params.append("team_id", teamId)

  const url = `/ppt/user-templates${params.toString() ? `?${params}` : ""}`
  const response = await api.get<ApiResponse<{ templates: UserTemplate[] }>>(url)
  return response.data
}

/**
 * Get user template details
 */
export const getUserTemplate = async (templateId: string): Promise<UserTemplate> => {
  const response = await api.get<ApiResponse<UserTemplate>>(
    `/ppt/user-templates/${templateId}`
  )
  return response.data
}

/**
 * Update user template
 */
export const updateUserTemplate = async (
  templateId: string,
  data: { name?: string; visibility?: "private" | "team" | "public" }
): Promise<UserTemplate> => {
  const response = await api.put<ApiResponse<UserTemplate>>(
    `/ppt/user-templates/${templateId}`,
    data
  )
  return response.data
}

/**
 * Delete user template
 */
export const deleteUserTemplate = async (templateId: string): Promise<void> => {
  await api.delete(`/ppt/user-templates/${templateId}`)
}

/**
 * Upload project template image
 */
export const uploadProjectTemplate = async (
  projectId: string,
  file: File
): Promise<{ template_image_url: string }> => {
  const formData = new FormData()
  formData.append("template_image", file)

  const response = await apiClient.post<ApiResponse<{ template_image_url: string }>>(
    `/ppt/projects/${projectId}/template`,
    formData
  )
  return response.data.data
}

/**
 * Delete project template
 */
export const deleteProjectTemplate = async (projectId: string): Promise<void> => {
  await api.delete(`/ppt/projects/${projectId}/template`)
}

// ===== Settings APIs =====

/**
 * Get user PPT settings
 */
export const getSettings = async (): Promise<PPTSettings> => {
  const response = await api.get<ApiResponse<PPTSettings>>("/ppt/settings")
  return response.data
}

/**
 * Update user PPT settings
 */
export const updateSettings = async (
  data: Partial<{
    ai_provider: string
    api_base_url: string
    api_key: string
    text_model: string
    image_model: string
    image_resolution: string
    image_aspect_ratio: string
    max_description_workers: number
    max_image_workers: number
    output_language: OutputLanguage
  }>
): Promise<PPTSettings> => {
  const response = await api.put<ApiResponse<PPTSettings>>("/ppt/settings", data)
  return response.data
}

/**
 * Reset settings to defaults
 */
export const resetSettings = async (): Promise<PPTSettings> => {
  const response = await api.post<ApiResponse<PPTSettings>>("/ppt/settings/reset")
  return response.data
}

/**
 * Validate API settings
 */
export const validateSettings = async (): Promise<{
  valid: boolean
  message: string
  using_default?: boolean
  provider?: string
}> => {
  const response = await api.post<
    ApiResponse<{
      valid: boolean
      message: string
      using_default?: boolean
      provider?: string
    }>
  >("/ppt/settings/validate")
  return response.data
}

/**
 * Get output language
 */
export const getOutputLanguage = async (): Promise<{ output_language: OutputLanguage }> => {
  const response = await api.get<ApiResponse<{ output_language: OutputLanguage }>>(
    "/ppt/output-language"
  )
  return response.data
}

/**
 * Set output language
 */
export const setOutputLanguage = async (
  language: OutputLanguage
): Promise<{ output_language: OutputLanguage }> => {
  const response = await api.put<ApiResponse<{ output_language: OutputLanguage }>>(
    "/ppt/output-language",
    { output_language: language }
  )
  return response.data
}

// ===== Task APIs =====

/**
 * List tasks for a project
 */
export const listProjectTasks = async (
  projectId: string,
  options?: { status?: string; task_type?: string; limit?: number; offset?: number }
): Promise<{
  tasks: Array<{
    id: string
    project_id: string
    task_type: string
    status: string
    progress: Record<string, unknown> | null
    error_message: string | null
    created_at: string
    completed_at: string | null
  }>
  has_more: boolean
  limit: number
  offset: number
}> => {
  const params = new URLSearchParams()
  if (options?.status) params.append("status", options.status)
  if (options?.task_type) params.append("task_type", options.task_type)
  if (options?.limit) params.append("limit", options.limit.toString())
  if (options?.offset) params.append("offset", options.offset.toString())

  const url = `/ppt/projects/${projectId}/tasks${params.toString() ? `?${params}` : ""}`
  const response = await api.get<ApiResponse<{
    tasks: Array<{
      id: string
      project_id: string
      task_type: string
      status: string
      progress: Record<string, unknown> | null
      error_message: string | null
      created_at: string
      completed_at: string | null
    }>
    has_more: boolean
    limit: number
    offset: number
  }>>(url)
  return response.data
}

/**
 * List all user tasks across projects
 */
export const listUserTasks = async (
  options?: { status?: string; task_type?: string; limit?: number; offset?: number }
): Promise<{
  tasks: Array<{
    id: string
    project_id: string
    task_type: string
    status: string
    progress: Record<string, unknown> | null
    error_message: string | null
    created_at: string
    completed_at: string | null
  }>
  has_more: boolean
  limit: number
  offset: number
}> => {
  const params = new URLSearchParams()
  if (options?.status) params.append("status", options.status)
  if (options?.task_type) params.append("task_type", options.task_type)
  if (options?.limit) params.append("limit", options.limit.toString())
  if (options?.offset) params.append("offset", options.offset.toString())

  const url = `/ppt/tasks${params.toString() ? `?${params}` : ""}`
  const response = await api.get<ApiResponse<{
    tasks: Array<{
      id: string
      project_id: string
      task_type: string
      status: string
      progress: Record<string, unknown> | null
      error_message: string | null
      created_at: string
      completed_at: string | null
    }>
    has_more: boolean
    limit: number
    offset: number
  }>>(url)
  return response.data
}

// ===== Image Version APIs =====

/**
 * Get page image versions
 */
export const getPageImageVersions = async (
  projectId: string,
  pageId: string
): Promise<{ versions: ImageVersion[] }> => {
  const response = await api.get<ApiResponse<{ versions: ImageVersion[] }>>(
    `/ppt/projects/${projectId}/pages/${pageId}/image-versions`
  )
  return response.data
}

/**
 * Set current image version
 */
export const setCurrentImageVersion = async (
  projectId: string,
  pageId: string,
  versionId: string
): Promise<void> => {
  await api.post(
    `/ppt/projects/${projectId}/pages/${pageId}/image-versions/${versionId}/set-current`
  )
}

// ===== Page APIs =====

/**
 * Add a new page to project
 */
export const addPage = async (
  projectId: string,
  data: {
    order_index?: number
    part?: string
    outline_content?: { title?: string; points?: string[] }
  }
): Promise<{
  id: string
  project_id: string
  order_index: number
  part: string | null
  outline_content: { title?: string; points?: string[] } | null
  description_content: unknown | null
  generated_image_url: string | null
  status: string
  created_at: string
  updated_at: string
}> => {
  const response = await api.post<
    ApiResponse<{
      id: string
      project_id: string
      order_index: number
      part: string | null
      outline_content: { title?: string; points?: string[] } | null
      description_content: unknown | null
      generated_image_url: string | null
      status: string
      created_at: string
      updated_at: string
    }>
  >(`/ppt/projects/${projectId}/pages`, data)
  return response.data
}

/**
 * Update page outline
 */
export const updatePageOutline = async (
  projectId: string,
  pageId: string,
  outlineContent: { title?: string; points?: string[] }
): Promise<unknown> => {
  const response = await api.put<ApiResponse<unknown>>(
    `/ppt/projects/${projectId}/pages/${pageId}/outline`,
    { outline_content: outlineContent }
  )
  return response.data
}

/**
 * Update page description
 */
export const updatePageDescription = async (
  projectId: string,
  pageId: string,
  descriptionContent: unknown
): Promise<unknown> => {
  const response = await api.put<ApiResponse<unknown>>(
    `/ppt/projects/${projectId}/pages/${pageId}/description`,
    { description_content: descriptionContent }
  )
  return response.data
}

/**
 * Generate single page description
 */
export const generatePageDescription = async (
  projectId: string,
  pageId: string,
  forceRegenerate = false,
  language?: OutputLanguage
): Promise<unknown> => {
  const response = await api.post<ApiResponse<unknown>>(
    `/ppt/projects/${projectId}/pages/${pageId}/generate/description`,
    { force_regenerate: forceRegenerate, language }
  )
  return response.data
}

/**
 * Generate single page image
 */
export const generatePageImage = async (
  projectId: string,
  pageId: string,
  forceRegenerate = false,
  language?: OutputLanguage
): Promise<unknown> => {
  const response = await api.post<ApiResponse<unknown>>(
    `/ppt/projects/${projectId}/pages/${pageId}/generate/image`,
    { force_regenerate: forceRegenerate, language }
  )
  return response.data
}

/**
 * Edit page image with natural language instruction
 * Supports optional selection mask and material images
 */
export const editPageImage = async (
  projectId: string,
  pageId: string,
  instruction: string,
  options?: {
    selectionMask?: string // Base64 encoded mask image
    materialImages?: string[] // Base64 encoded material images
  }
): Promise<{
  page: {
    id: string
    generated_image_url: string
    status: string
  }
  new_version: ImageVersion
}> => {
  const response = await api.post<
    ApiResponse<{
      page: {
        id: string
        generated_image_url: string
        status: string
      }
      new_version: ImageVersion
    }>
  >(`/ppt/projects/${projectId}/pages/${pageId}/edit-image`, {
    instruction,
    selection_mask: options?.selectionMask,
    material_images: options?.materialImages,
  })
  return response.data
}

/**
 * Edit page image with file uploads (for material images)
 */
export const editPageImageWithFiles = async (
  projectId: string,
  pageId: string,
  instruction: string,
  options?: {
    selectionMaskFile?: File
    materialImageFiles?: File[]
  }
): Promise<{
  page: {
    id: string
    generated_image_url: string
    status: string
  }
  new_version: ImageVersion
}> => {
  // Convert files to base64
  const toBase64 = (file: File): Promise<string> => {
    return new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.readAsDataURL(file)
      reader.onload = () => {
        const result = reader.result as string
        // Remove data URL prefix (e.g., "data:image/png;base64,")
        const base64 = result.split(",")[1]
        resolve(base64)
      }
      reader.onerror = reject
    })
  }

  let selectionMask: string | undefined
  let materialImages: string[] | undefined

  if (options?.selectionMaskFile) {
    selectionMask = await toBase64(options.selectionMaskFile)
  }

  if (options?.materialImageFiles && options.materialImageFiles.length > 0) {
    materialImages = await Promise.all(options.materialImageFiles.map(toBase64))
  }

  return editPageImage(projectId, pageId, instruction, {
    selectionMask,
    materialImages,
  })
}

/**
 * Reorder pages within a project
 */
export const reorderPages = async (
  projectId: string,
  pageIds: string[]
): Promise<{
  pages: Array<{
    id: string
    order_index: number
    outline_content: { title?: string; points?: string[] } | null
    description_content: unknown | null
    generated_image_url: string | null
    status: string
  }>
}> => {
  const response = await api.post<
    ApiResponse<{
      pages: Array<{
        id: string
        order_index: number
        outline_content: { title?: string; points?: string[] } | null
        description_content: unknown | null
        generated_image_url: string | null
        status: string
      }>
    }>
  >(`/ppt/projects/${projectId}/pages/reorder`, {
    page_ids: pageIds,
  })
  return response.data
}

/**
 * Update pages order (legacy alias for reorderPages)
 */
export const updatePagesOrder = reorderPages

// ===== Export APIs =====

export type ExportFormat = "pptx" | "pdf"
export type ExportMode = "simple" | "editable"

export interface TextElementInfo {
  id: string
  text: string
  bbox: [number, number, number, number]
  font_color: string
  is_bold: boolean
  is_italic: boolean
  is_underline: boolean
  font_size: number | null
  alignment: "left" | "center" | "right"
}

export interface ExportResult {
  download_url: string
  filename: string
  pages_exported: number
  mode: ExportMode
  format: ExportFormat
  text_elements?: Record<number, TextElementInfo[]> // page_index -> text elements (for editable mode)
}

/**
 * Unified export function for all formats and modes
 */
export const exportProject = async (
  projectId: string,
  options: {
    format: ExportFormat
    mode?: ExportMode
    filename?: string
  }
): Promise<ExportResult> => {
  const response = await api.post<ApiResponse<ExportResult>>(
    `/ppt/projects/${projectId}/export`,
    {
      format: options.format,
      mode: options.mode || "simple",
      filename: options.filename,
    }
  )
  return response.data
}

/**
 * Export as simple PPTX (images as background)
 */
export const exportPPTX = async (
  projectId: string,
  filename?: string
): Promise<ExportResult> => {
  return exportProject(projectId, {
    format: "pptx",
    mode: "simple",
    filename,
  })
}

/**
 * Export as PDF
 */
export const exportPDF = async (
  projectId: string,
  filename?: string
): Promise<ExportResult> => {
  return exportProject(projectId, {
    format: "pdf",
    mode: "simple",
    filename,
  })
}

/**
 * Export as editable PPTX (with extracted text boxes)
 */
export const exportEditablePPTX = async (
  projectId: string,
  filename?: string
): Promise<ExportResult> => {
  return exportProject(projectId, {
    format: "pptx",
    mode: "editable",
    filename,
  })
}

/**
 * Export as editable PDF (with extracted text)
 */
export const exportEditablePDF = async (
  projectId: string,
  filename?: string
): Promise<ExportResult> => {
  return exportProject(projectId, {
    format: "pdf",
    mode: "editable",
    filename,
  })
}

// ===== Refinement APIs =====

/**
 * Refine outline with AI
 */
export const refineOutline = async (
  projectId: string,
  userRequirement: string,
  previousRequirements?: string[],
  language?: OutputLanguage
): Promise<{
  pages: Array<{
    id: string
    order_index: number
    outline_content: { title?: string; points?: string[] }
  }>
  message: string
}> => {
  const response = await api.post<
    ApiResponse<{
      pages: Array<{
        id: string
        order_index: number
        outline_content: { title?: string; points?: string[] }
      }>
      message: string
    }>
  >(`/ppt/projects/${projectId}/refine/outline`, {
    user_requirement: userRequirement,
    previous_requirements: previousRequirements || [],
    language,
  })
  return response.data
}

/**
 * Refine descriptions with AI
 */
export const refineDescriptions = async (
  projectId: string,
  userRequirement: string,
  previousRequirements?: string[],
  language?: OutputLanguage
): Promise<{
  pages: Array<{
    id: string
    order_index: number
    description_content: unknown
  }>
  message: string
}> => {
  const response = await api.post<
    ApiResponse<{
      pages: Array<{
        id: string
        order_index: number
        description_content: unknown
      }>
      message: string
    }>
  >(`/ppt/projects/${projectId}/refine/descriptions`, {
    user_requirement: userRequirement,
    previous_requirements: previousRequirements || [],
    language,
  })
  return response.data
}

// ===== Material Generation =====

/**
 * Generate material image (async task)
 */
export const generateMaterialImage = async (
  projectId: string,
  prompt: string,
  refImage?: File | null,
  extraImages?: File[]
): Promise<{ task_id: string; status: string }> => {
  const formData = new FormData()
  formData.append("prompt", prompt)
  if (refImage) {
    formData.append("ref_image", refImage)
  }
  if (extraImages && extraImages.length > 0) {
    extraImages.forEach((file) => {
      formData.append("extra_images", file)
    })
  }

  const response = await apiClient.post<ApiResponse<{ task_id: string; status: string }>>(
    `/ppt/projects/${projectId}/materials/generate`,
    formData
  )
  return response.data.data
}
