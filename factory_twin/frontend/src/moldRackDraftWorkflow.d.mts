export type MoldRackDraftStatus = "none" | "draft" | "validated" | "published";

export interface MoldRackDraftWorkflowInput {
  busy?: boolean;
  hasDraft?: boolean;
  status?: MoldRackDraftStatus;
  hasUnsavedInput?: boolean;
}

export interface MoldRackDraftWorkflowAction {
  disabled: boolean;
  title: string;
}

export function moldRackDraftWorkflowState(input?: MoldRackDraftWorkflowInput): {
  save: MoldRackDraftWorkflowAction;
  validate: MoldRackDraftWorkflowAction;
  publish: MoldRackDraftWorkflowAction;
};
