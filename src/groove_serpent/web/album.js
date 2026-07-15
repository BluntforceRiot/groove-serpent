"use strict";

const WORKBENCH_SCHEMA = "groove-serpent.album-workbench/4";
const IDENTIFICATION_CATALOG_SCHEMA = "groove-serpent.album-identification-catalog/1";
const IDENTIFICATION_PROPOSAL_SCHEMA = "groove-serpent.album-identification-proposal/2";
const RELEASE_REVIEW_SCHEMA = "groove-serpent.album-release-review/1";
const ARTWORK_REVIEW_SCHEMA = "groove-serpent.album-artwork-review/1";
const PUBLICATION_CATALOG_SCHEMA = "groove-serpent.album-publication-plan-catalog/1";
const PUBLICATION_OPERATION_CATALOG_SCHEMA = "groove-serpent.album-publication-operation-catalog/1";
const PUBLICATION_PLAN_SUFFIX = ".publication-plan.json";
const IDENTITY_FIELDS = Object.freeze([
  "project_revision",
  "project_sha256",
  "editable_state_sha256",
  "source_sha256",
  "project_speed_state_sha256",
]);
const EDITABLE_METADATA_FIELDS = Object.freeze([
  "album",
  "album_artist",
  "artist",
  "year",
  "genre",
]);

const ACTION_GUIDANCE = Object.freeze({
  edit_album_metadata: "Add the missing album metadata in the album project, then refresh this workbench.",
  inspect_source: "Inspect the side project and its source capture. A source mismatch cannot be approved by repinning.",
  review_speed: "Review the selected album speed against the side project's reviewed speed state.",
  inherit_project_speed: "Choose the side project's reviewed speed in the album project if the override is not intentional.",
  resolve_metadata: "Resolve this album-versus-side metadata difference in the album or side project, then refresh.",
  review_side: "Open the side project and review the changed state before recording a new pin.",
  repin_side: "After reviewing the side, record its exact current identity with the approval control below.",
});

const elements = Object.freeze({
  workbench: document.getElementById("workbench"),
  refreshButton: document.getElementById("refreshButton"),
  albumTitle: document.getElementById("albumTitle"),
  albumArtist: document.getElementById("albumArtist"),
  albumPath: document.getElementById("albumPath"),
  readiness: document.getElementById("readiness"),
  readinessValue: document.getElementById("readinessValue"),
  readinessNote: document.getElementById("readinessNote"),
  globalNotice: document.getElementById("globalNotice"),
  globalNoticeText: document.getElementById("globalNoticeText"),
  noticeReloadButton: document.getElementById("noticeReloadButton"),
  loadFailure: document.getElementById("loadFailure"),
  loadFailureMessage: document.getElementById("loadFailureMessage"),
  retryButton: document.getElementById("retryButton"),
  workbenchContent: document.getElementById("workbenchContent"),
  sideSummary: document.getElementById("sideSummary"),
  sideCards: document.getElementById("sideCards"),
  pairingEditor: document.getElementById("pairingEditor"),
  albumDetailsForm: document.getElementById("albumDetailsForm"),
  metadataAlbum: document.getElementById("metadataAlbum"),
  metadataAlbumArtist: document.getElementById("metadataAlbumArtist"),
  metadataArtist: document.getElementById("metadataArtist"),
  metadataYear: document.getElementById("metadataYear"),
  metadataGenre: document.getElementById("metadataGenre"),
  artworkPath: document.getElementById("artworkPath"),
  saveDetailsButton: document.getElementById("saveDetailsButton"),
  detailsStatus: document.getElementById("detailsStatus"),
  addSideForm: document.getElementById("addSideForm"),
  newSideLabel: document.getElementById("newSideLabel"),
  newSideProject: document.getElementById("newSideProject"),
  addSideButton: document.getElementById("addSideButton"),
  addSideStatus: document.getElementById("addSideStatus"),
  orderApprovalCheckbox: document.getElementById("orderApprovalCheckbox"),
  orderPolicyReason: document.getElementById("orderPolicyReason"),
  orderStatus: document.getElementById("orderStatus"),
  removeSideDialog: document.getElementById("removeSideDialog"),
  removeSideForm: document.getElementById("removeSideForm"),
  removeSideHeading: document.getElementById("removeSideHeading"),
  removeSideCopy: document.getElementById("removeSideCopy"),
  removeSidePhrase: document.getElementById("removeSidePhrase"),
  removeSideConfirmation: document.getElementById("removeSideConfirmation"),
  removeSideStatus: document.getElementById("removeSideStatus"),
  cancelRemoveSide: document.getElementById("cancelRemoveSide"),
  confirmRemoveSide: document.getElementById("confirmRemoveSide"),
  attentionHeading: document.getElementById("attentionHeading"),
  attentionTotal: document.getElementById("attentionTotal"),
  blockerCount: document.getElementById("blockerCount"),
  reviewCount: document.getElementById("reviewCount"),
  queueHelp: document.getElementById("queueHelp"),
  exceptionQueue: document.getElementById("exceptionQueue"),
  exceptionDetail: document.getElementById("exceptionDetail"),
  detailEmpty: document.getElementById("detailEmpty"),
  detailEmptyHeading: document.getElementById("detailEmptyHeading"),
  detailEmptyCopy: document.getElementById("detailEmptyCopy"),
  detailContent: document.getElementById("detailContent"),
  detailContext: document.getElementById("detailContext"),
  detailHeading: document.getElementById("detailHeading"),
  detailSeverity: document.getElementById("detailSeverity"),
  detailMessage: document.getElementById("detailMessage"),
  detailFacts: document.getElementById("detailFacts"),
  evidenceGrid: document.getElementById("evidenceGrid"),
  repinPanel: document.getElementById("repinPanel"),
  repinForm: document.getElementById("repinForm"),
  reviewedCheckbox: document.getElementById("reviewedCheckbox"),
  repinButton: document.getElementById("repinButton"),
  repinStatus: document.getElementById("repinStatus"),
  manualActionPanel: document.getElementById("manualActionPanel"),
  manualActionCopy: document.getElementById("manualActionCopy"),
  identificationReadiness: document.getElementById("identificationReadiness"),
  identificationScanForm: document.getElementById("identificationScanForm"),
  identificationStatus: document.getElementById("identificationStatus"),
  identificationProvider: document.getElementById("identificationProvider"),
  identificationNetworkReviewed: document.getElementById("identificationNetworkReviewed"),
  identificationScanButton: document.getElementById("identificationScanButton"),
  identificationCatalogSummary: document.getElementById("identificationCatalogSummary"),
  identificationCatalogHeading: document.getElementById("identificationCatalogHeading"),
  identificationCatalog: document.getElementById("identificationCatalog"),
  identificationReview: document.getElementById("identificationReview"),
  identificationReviewHeading: document.getElementById("identificationReviewHeading"),
  closeIdentificationReview: document.getElementById("closeIdentificationReview"),
  identificationDecision: document.getElementById("identificationDecision"),
  identificationCandidates: document.getElementById("identificationCandidates"),
  releaseDetailsNetworkReviewed: document.getElementById("releaseDetailsNetworkReviewed"),
  releaseDetailsStatus: document.getElementById("releaseDetailsStatus"),
  releaseDetailsReview: document.getElementById("releaseDetailsReview"),
  releaseDetailsHeading: document.getElementById("releaseDetailsHeading"),
  releaseDetailsContent: document.getElementById("releaseDetailsContent"),
  copyReleaseMetadataButton: document.getElementById("copyReleaseMetadataButton"),
  artworkNetworkReviewed: document.getElementById("artworkNetworkReviewed"),
  downloadArtworkButton: document.getElementById("downloadArtworkButton"),
  artworkReviewStatus: document.getElementById("artworkReviewStatus"),
  artworkReview: document.getElementById("artworkReview"),
  identificationPressing: document.getElementById("identificationPressing"),
  publicationReadiness: document.getElementById("publicationReadiness"),
  publicationPlanForm: document.getElementById("publicationPlanForm"),
  publicationPlanStatus: document.getElementById("publicationPlanStatus"),
  publicationPlanFilename: document.getElementById("publicationPlanFilename"),
  publicationPlanFilenamePreview: document.getElementById("publicationPlanFilenamePreview"),
  publicationProfiles: document.getElementById("publicationProfiles"),
  publicationRestorationModes: document.getElementById("publicationRestorationModes"),
  publicationFlacCompression: document.getElementById("publicationFlacCompression"),
  publicationAacBitrate: document.getElementById("publicationAacBitrate"),
  publicationReviewed: document.getElementById("publicationReviewed"),
  createPublicationPlanButton: document.getElementById("createPublicationPlanButton"),
  publicationCatalogSummary: document.getElementById("publicationCatalogSummary"),
  publicationCatalog: document.getElementById("publicationCatalog"),
  publicationOperationSummary: document.getElementById("publicationOperationSummary"),
  publicationExecutionForm: document.getElementById("publicationExecutionForm"),
  publicationExecutionPlan: document.getElementById("publicationExecutionPlan"),
  publicationDestinationName: document.getElementById("publicationDestinationName"),
  publicationDestinationPreview: document.getElementById("publicationDestinationPreview"),
  publicationExecutionPhrase: document.getElementById("publicationExecutionPhrase"),
  publicationExecutionConfirmation: document.getElementById("publicationExecutionConfirmation"),
  publicationExecutionConfirmed: document.getElementById("publicationExecutionConfirmed"),
  executePublicationButton: document.getElementById("executePublicationButton"),
  publicationExecutionStatus: document.getElementById("publicationExecutionStatus"),
  publicationProgress: document.getElementById("publicationProgress"),
  publicationReceipts: document.getElementById("publicationReceipts"),
  publicationReplayForm: document.getElementById("publicationReplayForm"),
  publicationReplaySource: document.getElementById("publicationReplaySource"),
  publicationReplayDestination: document.getElementById("publicationReplayDestination"),
  publicationReplayPreview: document.getElementById("publicationReplayPreview"),
  publicationReplayPhrase: document.getElementById("publicationReplayPhrase"),
  publicationReplayConfirmation: document.getElementById("publicationReplayConfirmation"),
  publicationReplayConfirmed: document.getElementById("publicationReplayConfirmed"),
  cancelPublicationReplay: document.getElementById("cancelPublicationReplay"),
  replayPublicationButton: document.getElementById("replayPublicationButton"),
  publicationReplayStatus: document.getElementById("publicationReplayStatus"),
  publicationRecoveryStatus: document.getElementById("publicationRecoveryStatus"),
  publicationOrphans: document.getElementById("publicationOrphans"),
  publicationRecoveryDialog: document.getElementById("publicationRecoveryDialog"),
  publicationRecoveryForm: document.getElementById("publicationRecoveryForm"),
  publicationRecoveryDialogHeading: document.getElementById("publicationRecoveryDialogHeading"),
  publicationRecoveryCopy: document.getElementById("publicationRecoveryCopy"),
  publicationRecoveryPhrase: document.getElementById("publicationRecoveryPhrase"),
  publicationRecoveryConfirmation: document.getElementById("publicationRecoveryConfirmation"),
  publicationRecoveryConfirmed: document.getElementById("publicationRecoveryConfirmed"),
  publicationRecoveryDialogStatus: document.getElementById("publicationRecoveryDialogStatus"),
  cancelPublicationRecovery: document.getElementById("cancelPublicationRecovery"),
  confirmPublicationRecovery: document.getElementById("confirmPublicationRecovery"),
});

let albumState = null;
let selectedExceptionId = null;
let loadGeneration = 0;
let loadController = null;
let mutationBusy = false;
let staleState = false;
const openingSides = new Set();
const sideOpenButtons = new Map();
const sideOpenStatuses = new Map();
const sideOrderButtons = new Map();
const sideRemoveButtons = new Map();
let pendingRemoveSide = null;
let identificationBusy = false;
let selectedIdentificationProposal = null;
let selectedReleaseReview = null;
let selectedArtworkReview = null;
let reviewedArtworkSelection = null;
let publicationBusy = false;
let selectedExecutionPlanSha256 = null;
let selectedReplayReceipt = null;
let pendingPublicationRecovery = null;

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value, expected) {
  if (!isObject(value)) return false;
  const keys = Object.keys(value);
  return keys.length === expected.length && expected.every((key) => Object.hasOwn(value, key));
}

function isDigest(value) {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function isMbid(value) {
  return typeof value === "string"
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

function asText(value, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function asCount(value, fallback = 0) {
  return Number.isSafeInteger(value) && value >= 0 ? value : fallback;
}

function formatLabel(value) {
  return asText(value, "value")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function plural(count, singular, pluralValue = `${singular}s`) {
  return `${count.toLocaleString()} ${count === 1 ? singular : pluralValue}`;
}

function createElement(tagName, className, textValue) {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  if (textValue !== undefined) element.textContent = textValue;
  return element;
}

function replaceChildren(element, children = []) {
  element.replaceChildren(...children);
}

function exceptionCounts(state = albumState) {
  const exceptions = Array.isArray(state?.exceptions) ? state.exceptions : [];
  return {
    total: exceptions.length,
    blockers: exceptions.filter((item) => item.severity === "blocker").length,
    reviews: exceptions.filter((item) => item.severity === "review").length,
  };
}

function validateIdentity(identity, label) {
  if (!isObject(identity)) throw new Error(`${label} is missing its current identity.`);
  const keys = Object.keys(identity);
  if (keys.length !== IDENTITY_FIELDS.length || IDENTITY_FIELDS.some((field) => !keys.includes(field))) {
    throw new Error(`${label} has an incomplete current identity.`);
  }
  const result = {};
  for (const field of IDENTITY_FIELDS) result[field] = identity[field];
  if (!Number.isSafeInteger(result.project_revision) || result.project_revision <= 0) {
    throw new Error(`${label} has an invalid project revision.`);
  }
  for (const field of IDENTITY_FIELDS.filter((item) => item !== "project_revision")) {
    if (!/^[0-9a-f]{64}$/.test(result[field])) throw new Error(`${label} has an invalid ${formatLabel(field)}.`);
  }
  return result;
}

function validatePublicationOperations(operations, albumSha256) {
  if (
    !hasExactKeys(operations, ["schema", "album_sha256", "scan_complete", "summary", "publications", "orphans", "issues"])
    || operations.schema !== PUBLICATION_OPERATION_CATALOG_SCHEMA
    || operations.album_sha256 !== albumSha256
    || typeof operations.scan_complete !== "boolean"
    || !hasExactKeys(operations.summary, ["publications", "current", "stale", "invalid", "orphans", "actionable_orphans", "unsafe_orphans"])
    || Object.values(operations.summary).some((value) => !Number.isSafeInteger(value) || value < 0)
    || !Array.isArray(operations.publications)
    || operations.publications.length > 128
    || !Array.isArray(operations.orphans)
    || operations.orphans.length > 256
    || !Array.isArray(operations.issues)
  ) {
    throw new Error("The server returned an invalid publication-operation catalog.");
  }
  for (const issue of operations.issues) {
    if (!hasExactKeys(issue, ["code", "message"]) || typeof issue.code !== "string" || typeof issue.message !== "string") {
      throw new Error("The server returned an invalid publication-operation issue.");
    }
  }
  for (const receipt of operations.publications) {
    if (
      !hasExactKeys(receipt, [
        "directory_name",
        "status",
        "plan_filename",
        "plan_file_sha256",
        "plan_sha256",
        "album_sha256",
        "manifest_sha256",
        "journal_sha256",
        "artifact_count",
        "issues",
      ])
      || typeof receipt.directory_name !== "string"
      || !["current", "stale", "invalid"].includes(receipt.status)
      || (receipt.plan_filename !== null && typeof receipt.plan_filename !== "string")
      || ["plan_file_sha256", "plan_sha256", "album_sha256", "manifest_sha256", "journal_sha256"]
        .some((field) => receipt[field] !== null && !isDigest(receipt[field]))
      || !Number.isSafeInteger(receipt.artifact_count)
      || receipt.artifact_count < 0
      || !Array.isArray(receipt.issues)
    ) {
      throw new Error("The server returned an invalid final-publication receipt.");
    }
    for (const issue of receipt.issues) {
      if (!hasExactKeys(issue, ["code", "message"]) || typeof issue.code !== "string" || typeof issue.message !== "string") {
        throw new Error("The server returned an invalid final-publication issue.");
      }
    }
  }
  for (const orphan of operations.orphans) {
    if (
      !hasExactKeys(orphan, [
        "directory_name",
        "kind",
        "owned",
        "state",
        "plan_sha256",
        "intended_output_name",
        "journal_sha256",
        "directory_identity",
        "file_count",
        "total_size_bytes",
        "issue",
        "belongs_to_album",
        "matches_current_plan",
        "actionable",
      ])
      || typeof orphan.directory_name !== "string"
      || !["partial", "quarantine"].includes(orphan.kind)
      || typeof orphan.owned !== "boolean"
      || (orphan.state !== null && typeof orphan.state !== "string")
      || (orphan.plan_sha256 !== null && !isDigest(orphan.plan_sha256))
      || (orphan.intended_output_name !== null && typeof orphan.intended_output_name !== "string")
      || (orphan.journal_sha256 !== null && !isDigest(orphan.journal_sha256))
      || !Number.isSafeInteger(orphan.file_count)
      || orphan.file_count < 0
      || !Number.isSafeInteger(orphan.total_size_bytes)
      || orphan.total_size_bytes < 0
      || (orphan.issue !== null && typeof orphan.issue !== "string")
      || typeof orphan.belongs_to_album !== "boolean"
      || typeof orphan.matches_current_plan !== "boolean"
      || typeof orphan.actionable !== "boolean"
    ) {
      throw new Error("The server returned an invalid publication-orphan receipt.");
    }
    if (orphan.directory_identity !== null) {
      if (
        !hasExactKeys(orphan.directory_identity, ["device", "inode", "file_type", "birth_ns", "file_attributes"])
        || ["device", "inode", "file_type"].some((field) => !/^\d{1,20}$/.test(orphan.directory_identity[field]))
        || ["birth_ns", "file_attributes"].some((field) => orphan.directory_identity[field] !== null && !/^\d{1,20}$/.test(orphan.directory_identity[field]))
      ) {
        throw new Error("The server returned an invalid publication-orphan directory identity.");
      }
    }
    if (orphan.actionable && (!orphan.owned || !orphan.belongs_to_album || !isDigest(orphan.journal_sha256) || !orphan.directory_identity)) {
      throw new Error("The server marked an unproven publication orphan actionable.");
    }
  }
  return operations;
}

function validatePublicationState(publication, albumSha256) {
  if (!hasExactKeys(publication, ["readiness", "choices", "default_plan_filename", "default_destination_name", "catalog", "operations", "authority"])) {
    throw new Error("The server returned an invalid publication-planning state.");
  }
  if (
    !hasExactKeys(publication.readiness, [
      "can_create_plan",
      "can_preflight_current_plan",
      "can_execute_current_plan",
      "can_verify_publication",
      "can_replay_current_publication",
      "can_recover_owned_orphan",
      "reason_codes",
      "execution_reason_codes",
    ])
    || typeof publication.readiness.can_create_plan !== "boolean"
    || typeof publication.readiness.can_preflight_current_plan !== "boolean"
    || typeof publication.readiness.can_execute_current_plan !== "boolean"
    || typeof publication.readiness.can_verify_publication !== "boolean"
    || typeof publication.readiness.can_replay_current_publication !== "boolean"
    || typeof publication.readiness.can_recover_owned_orphan !== "boolean"
    || !Array.isArray(publication.readiness.reason_codes)
    || publication.readiness.reason_codes.some((item) => typeof item !== "string")
    || !Array.isArray(publication.readiness.execution_reason_codes)
    || publication.readiness.execution_reason_codes.some((item) => typeof item !== "string")
  ) {
    throw new Error("The server returned invalid publication readiness.");
  }
  if (
    !hasExactKeys(publication.authority, [
      "automatic_plan_creation",
      "review_required",
      "automatic_execution",
      "owner_confirmation_required",
      "execution_available_here",
      "overwrite_allowed",
      "destinations_restricted_to_album_folder",
      "resume_available",
    ])
    || publication.authority.automatic_plan_creation !== false
    || publication.authority.review_required !== true
    || publication.authority.automatic_execution !== false
    || publication.authority.owner_confirmation_required !== true
    || publication.authority.execution_available_here !== true
    || publication.authority.overwrite_allowed !== false
    || publication.authority.destinations_restricted_to_album_folder !== true
    || publication.authority.resume_available !== false
  ) {
    throw new Error("The server returned an unsupported publication authority policy.");
  }
  if (
    typeof publication.default_plan_filename !== "string"
    || !publication.default_plan_filename.endsWith(PUBLICATION_PLAN_SUFFIX)
    || typeof publication.default_destination_name !== "string"
    || !publication.default_destination_name
  ) {
    throw new Error("The server returned an invalid default publication-plan filename.");
  }
  if (!hasExactKeys(publication.choices, ["profiles", "restoration_modes", "flac_compression", "aac_bitrate_kbps"])) {
    throw new Error("The server returned invalid publication choices.");
  }
  if (
    !Array.isArray(publication.choices.profiles)
    || publication.choices.profiles.length !== 4
    || !Array.isArray(publication.choices.restoration_modes)
    || publication.choices.restoration_modes.length !== 2
  ) {
    throw new Error("The server returned incomplete publication choices.");
  }
  for (const profile of publication.choices.profiles) {
    if (
      !hasExactKeys(profile, ["id", "label", "description", "requires_reviewed_restoration"])
      || typeof profile.id !== "string"
      || typeof profile.label !== "string"
      || typeof profile.description !== "string"
      || typeof profile.requires_reviewed_restoration !== "boolean"
    ) {
      throw new Error("The server returned an invalid publication profile choice.");
    }
  }
  for (const mode of publication.choices.restoration_modes) {
    if (
      !hasExactKeys(mode, ["id", "label", "description"])
      || !["none", "reviewed"].includes(mode.id)
      || typeof mode.label !== "string"
      || typeof mode.description !== "string"
    ) {
      throw new Error("The server returned an invalid restoration choice.");
    }
  }
  for (const [name, minimum, maximum] of [
    ["flac_compression", 0, 12],
    ["aac_bitrate_kbps", 64, 512],
  ]) {
    const setting = publication.choices[name];
    if (
      !hasExactKeys(setting, ["default", "minimum", "maximum"])
      || setting.minimum !== minimum
      || setting.maximum !== maximum
      || !Number.isSafeInteger(setting.default)
      || setting.default < minimum
      || setting.default > maximum
    ) {
      throw new Error(`The server returned invalid ${formatLabel(name)} choices.`);
    }
  }
  const catalog = publication.catalog;
  if (
    !hasExactKeys(catalog, ["schema", "album_reference", "album_sha256", "scan_complete", "summary", "entries", "issues"])
    || catalog.schema !== PUBLICATION_CATALOG_SCHEMA
    || catalog.album_sha256 !== albumSha256
    || typeof catalog.album_reference !== "string"
    || typeof catalog.scan_complete !== "boolean"
    || !hasExactKeys(catalog.summary, ["total", "current", "stale", "invalid"])
    || !Array.isArray(catalog.entries)
    || catalog.entries.length > 128
    || !Array.isArray(catalog.issues)
  ) {
    throw new Error("The server returned an invalid publication-plan catalog.");
  }
  for (const entry of catalog.entries) {
    if (
      !hasExactKeys(entry, ["filename", "status", "file_sha256", "plan_sha256", "selected_profiles", "restoration_mode", "side_count", "issues"])
      || typeof entry.filename !== "string"
      || !["current", "stale", "invalid"].includes(entry.status)
      || (entry.file_sha256 !== null && !isDigest(entry.file_sha256))
      || (entry.plan_sha256 !== null && !isDigest(entry.plan_sha256))
      || !Array.isArray(entry.selected_profiles)
      || entry.selected_profiles.some((item) => typeof item !== "string")
      || ![null, "none", "reviewed"].includes(entry.restoration_mode)
      || (entry.side_count !== null && (!Number.isSafeInteger(entry.side_count) || entry.side_count < 1))
      || !Array.isArray(entry.issues)
    ) {
      throw new Error("The server returned an invalid publication-plan entry.");
    }
    for (const issue of entry.issues) {
      if (!hasExactKeys(issue, ["code", "message"]) || typeof issue.code !== "string" || typeof issue.message !== "string") {
        throw new Error("The server returned an invalid publication-plan issue.");
      }
    }
  }
  validatePublicationOperations(publication.operations, albumSha256);
  return publication;
}

function validateIdentificationIssue(issue) {
  if (
    !hasExactKeys(issue, ["code", "message"])
    || typeof issue.code !== "string"
    || typeof issue.message !== "string"
  ) {
    throw new Error("The server returned an invalid identification-catalog issue.");
  }
  return issue;
}

function validateIdentificationCatalog(catalog, albumSha256) {
  if (
    !hasExactKeys(catalog, [
      "schema",
      "album_reference",
      "album_sha256",
      "live_context_available",
      "scan_complete",
      "summary",
      "entries",
      "issues",
    ])
    || catalog.schema !== IDENTIFICATION_CATALOG_SCHEMA
    || typeof catalog.album_reference !== "string"
    || catalog.album_sha256 !== albumSha256
    || typeof catalog.live_context_available !== "boolean"
    || typeof catalog.scan_complete !== "boolean"
    || !hasExactKeys(catalog.summary, ["total", "current", "stale", "invalid", "selectable"])
    || !Array.isArray(catalog.entries)
    || catalog.entries.length > 128
    || !Array.isArray(catalog.issues)
  ) {
    throw new Error("The server returned an invalid identification-proposal catalog.");
  }
  for (const key of ["total", "current", "stale", "invalid", "selectable"]) {
    if (!Number.isSafeInteger(catalog.summary[key]) || catalog.summary[key] < 0) {
      throw new Error("The identification catalog returned invalid summary counts.");
    }
  }
  if (
    catalog.summary.total !== catalog.entries.length
    || catalog.summary.current + catalog.summary.stale + catalog.summary.invalid
      !== catalog.summary.total
    || catalog.summary.selectable !== catalog.summary.current
  ) {
    throw new Error("The identification catalog summary does not match its entries.");
  }
  for (const issue of catalog.issues) validateIdentificationIssue(issue);
  for (const entry of catalog.entries) {
    if (
      !hasExactKeys(entry, [
        "filename",
        "status",
        "selectable",
        "file_sha256",
        "proposal_sha256",
        "decision_status",
        "confidence",
        "selected_release_mbid",
        "manual_candidate_count",
        "issues",
      ])
      || typeof entry.filename !== "string"
      || !/^album-identification-[0-9a-f]{64}\.proposal\.json$/.test(entry.filename)
      || !["current", "stale", "invalid"].includes(entry.status)
      || entry.selectable !== (entry.status === "current")
      || (entry.file_sha256 !== null && !isDigest(entry.file_sha256))
      || (entry.proposal_sha256 !== null && !isDigest(entry.proposal_sha256))
      || (entry.decision_status !== null
        && !["proposed", "ambiguous", "abstained"].includes(entry.decision_status))
      || (entry.confidence !== null
        && !["high", "medium", "low", "none"].includes(entry.confidence))
      || (entry.selected_release_mbid !== null && !isMbid(entry.selected_release_mbid))
      || (entry.manual_candidate_count !== null
        && (!Number.isSafeInteger(entry.manual_candidate_count)
          || entry.manual_candidate_count < 0
          || entry.manual_candidate_count > 64))
      || !Array.isArray(entry.issues)
    ) {
      throw new Error("The server returned an invalid identification-proposal entry.");
    }
    for (const issue of entry.issues) validateIdentificationIssue(issue);
  }
  return catalog;
}

function validateIdentificationState(identification, albumSha256) {
  if (
    !hasExactKeys(identification, ["readiness", "provider", "catalog", "authority"])
    || !hasExactKeys(identification.readiness, ["can_scan", "reason_codes"])
    || typeof identification.readiness.can_scan !== "boolean"
    || !Array.isArray(identification.readiness.reason_codes)
    || identification.readiness.reason_codes.some((item) => typeof item !== "string")
    || !hasExactKeys(identification.provider, [
      "provider",
      "enabled",
      "ready",
      "message",
      "missing",
      "fingerprint_backend",
    ])
    || typeof identification.provider.provider !== "string"
    || typeof identification.provider.enabled !== "boolean"
    || typeof identification.provider.ready !== "boolean"
    || typeof identification.provider.message !== "string"
    || !Array.isArray(identification.provider.missing)
    || identification.provider.missing.some((item) => typeof item !== "string")
    || typeof identification.provider.fingerprint_backend !== "string"
  ) {
    throw new Error("The server returned invalid album-identification readiness.");
  }
  if (
    !hasExactKeys(identification.authority, [
      "automatic_network_requests",
      "explicit_network_review_required",
      "automatic_metadata_application",
      "automatic_artwork_download_or_application",
      "may_modify_album_project",
      "may_modify_side_projects",
      "physical_pressing_proven",
      "human_review_required",
    ])
    || identification.authority.automatic_network_requests !== false
    || identification.authority.explicit_network_review_required !== true
    || identification.authority.automatic_metadata_application !== false
    || identification.authority.automatic_artwork_download_or_application !== false
    || identification.authority.may_modify_album_project !== false
    || identification.authority.may_modify_side_projects !== false
    || identification.authority.physical_pressing_proven !== false
    || identification.authority.human_review_required !== true
  ) {
    throw new Error("The server returned an unsafe album-identification authority policy.");
  }
  validateIdentificationCatalog(identification.catalog, albumSha256);
  return identification;
}

function validateIdentificationProposal(proposal, albumSha256) {
  if (
    !hasExactKeys(proposal, [
      "schema",
      "algorithm",
      "album",
      "evidence",
      "config",
      "ranked_release_candidates",
      "excluded_conflicts",
      "decision",
      "exact_pressing_review",
      "authority",
      "proposal_sha256",
    ])
    || proposal.schema !== IDENTIFICATION_PROPOSAL_SCHEMA
    || !isDigest(proposal.proposal_sha256)
    || !isObject(proposal.album)
    || proposal.album.album_sha256 !== albumSha256
    || !Number.isSafeInteger(proposal.album.album_revision)
    || !isObject(proposal.evidence)
    || !Number.isSafeInteger(proposal.evidence.observed_track_count)
    || !Number.isSafeInteger(proposal.evidence.album_track_count)
    || proposal.evidence.observed_track_count < 1
    || proposal.evidence.observed_track_count > proposal.evidence.album_track_count
    || !Array.isArray(proposal.ranked_release_candidates)
    || proposal.ranked_release_candidates.length > 64
    || !Array.isArray(proposal.excluded_conflicts)
    || !hasExactKeys(proposal.decision, [
      "status",
      "confidence",
      "selected_release_mbid",
      "rank_margin",
      "reasons",
    ])
    || !["proposed", "ambiguous", "abstained"].includes(proposal.decision.status)
    || !["high", "medium", "low", "none"].includes(proposal.decision.confidence)
    || (proposal.decision.selected_release_mbid !== null
      && !isMbid(proposal.decision.selected_release_mbid))
    || (proposal.decision.rank_margin !== null
      && (!Number.isFinite(proposal.decision.rank_margin)
        || proposal.decision.rank_margin < 0
        || proposal.decision.rank_margin > 1))
    || !Array.isArray(proposal.decision.reasons)
    || proposal.decision.reasons.some((item) => typeof item !== "string")
  ) {
    throw new Error("The server returned an invalid album-identification proposal.");
  }
  if (
    !hasExactKeys(proposal.album, [
      "album_reference",
      "album_sha256",
      "album_revision",
      "context_sha256",
      "side_count",
      "track_count",
      "sides",
    ])
    || typeof proposal.album.album_reference !== "string"
    || !isDigest(proposal.album.context_sha256)
    || !Number.isSafeInteger(proposal.album.side_count)
    || proposal.album.side_count < 1
    || !Number.isSafeInteger(proposal.album.track_count)
    || proposal.album.track_count < 1
    || !Array.isArray(proposal.album.sides)
    || proposal.album.sides.length !== proposal.album.side_count
  ) {
    throw new Error("The identification proposal returned invalid album bindings.");
  }
  for (const side of proposal.album.sides) {
    if (
      !hasExactKeys(side, [
        "label",
        "order",
        "project_reference",
        "project_sha256",
        "project_revision",
        "project_state_sha256",
        "source_sha256",
        "source_size_bytes",
        "source_sample_rate",
        "speed_state_sha256",
        "requested_speed_factor",
        "fingerprint_asetrate_hz",
        "fingerprint_effective_speed_factor",
        "fingerprint_speed_transform",
        "track_count",
        "track_ranges_sha256",
        "tracks",
      ])
      || typeof side.label !== "string"
      || !Number.isSafeInteger(side.order)
      || typeof side.project_reference !== "string"
      || !isDigest(side.project_sha256)
      || !Number.isSafeInteger(side.project_revision)
      || !isDigest(side.project_state_sha256)
      || !isDigest(side.source_sha256)
      || !Number.isSafeInteger(side.source_size_bytes)
      || !Number.isSafeInteger(side.source_sample_rate)
      || !isDigest(side.speed_state_sha256)
      || !Number.isFinite(side.requested_speed_factor)
      || !Number.isSafeInteger(side.fingerprint_asetrate_hz)
      || !Number.isFinite(side.fingerprint_effective_speed_factor)
      || side.fingerprint_speed_transform !== "integer-asetrate-pitch-and-tempo/1"
      || !Number.isSafeInteger(side.track_count)
      || !isDigest(side.track_ranges_sha256)
      || !Array.isArray(side.tracks)
      || side.tracks.length !== side.track_count
    ) {
      throw new Error("The identification proposal returned invalid source-speed bindings.");
    }
    for (const track of side.tracks) {
      if (
        !hasExactKeys(track, ["number", "start_sample", "end_sample", "track_sha256"])
        || !Number.isSafeInteger(track.number)
        || !Number.isSafeInteger(track.start_sample)
        || !Number.isSafeInteger(track.end_sample)
        || track.end_sample <= track.start_sample
        || !isDigest(track.track_sha256)
      ) {
        throw new Error("The identification proposal returned invalid track bindings.");
      }
    }
  }
  for (const candidate of proposal.ranked_release_candidates) {
    if (
      !hasExactKeys(candidate, [
        "rank",
        "release",
        "release_mbid",
        "evidence_score",
        "mean_recognition_score",
        "supporting_track_count",
        "supporting_side_count",
        "album_track_coverage",
        "support",
        "pressing_identity_status",
      ])
      || !Number.isSafeInteger(candidate.rank)
      || candidate.rank < 1
      || !isObject(candidate.release)
      || !isMbid(candidate.release_mbid)
      || candidate.release.release_mbid !== candidate.release_mbid
      || typeof candidate.release.title !== "string"
      || !Number.isFinite(candidate.evidence_score)
      || candidate.evidence_score < 0
      || candidate.evidence_score > 1
      || !Number.isFinite(candidate.mean_recognition_score)
      || candidate.mean_recognition_score < 0
      || candidate.mean_recognition_score > 1
      || !Number.isSafeInteger(candidate.supporting_track_count)
      || candidate.supporting_track_count < 1
      || !Number.isSafeInteger(candidate.supporting_side_count)
      || candidate.supporting_side_count < 1
      || !Number.isFinite(candidate.album_track_coverage)
      || candidate.album_track_coverage <= 0
      || candidate.album_track_coverage > 1
      || !Array.isArray(candidate.support)
      || candidate.pressing_identity_status !== "candidate-not-proven"
    ) {
      throw new Error("The server returned an invalid ranked release candidate.");
    }
  }
  const pressing = proposal.exact_pressing_review;
  if (
    !hasExactKeys(pressing, [
      "proof_status",
      "database_release_id_is_physical_pressing_proof",
      "top_ranked_known_facts",
      "missing_or_unverified_facts",
      "owner_checks_required",
      "manual_candidates",
      "manual_candidates_affect_automatic_ranking",
    ])
    || pressing.proof_status !== "not-proven"
    || pressing.database_release_id_is_physical_pressing_proof !== false
    || !isObject(pressing.top_ranked_known_facts)
    || !Array.isArray(pressing.missing_or_unverified_facts)
    || !Array.isArray(pressing.owner_checks_required)
    || !Array.isArray(pressing.manual_candidates)
    || pressing.manual_candidates_affect_automatic_ranking !== false
  ) {
    throw new Error("The server returned invalid physical-pressing review evidence.");
  }
  if (
    !hasExactKeys(proposal.authority, [
      "may_modify_album_project",
      "may_modify_side_projects",
      "may_apply_metadata",
      "may_download_or_apply_artwork",
      "may_change_topology_speed_or_restoration",
      "may_publish",
      "human_review_required",
      "physical_pressing_proven",
    ])
    || proposal.authority.may_modify_album_project !== false
    || proposal.authority.may_modify_side_projects !== false
    || proposal.authority.may_apply_metadata !== false
    || proposal.authority.may_download_or_apply_artwork !== false
    || proposal.authority.may_change_topology_speed_or_restoration !== false
    || proposal.authority.may_publish !== false
    || proposal.authority.human_review_required !== true
    || proposal.authority.physical_pressing_proven !== false
  ) {
    throw new Error("The identification proposal grants unsupported authority.");
  }
  return proposal;
}

function validateReleaseReviewBinding(binding, proposal, entry, releaseMbid) {
  if (
    !hasExactKeys(binding, [
      "album_reference",
      "album_sha256",
      "album_revision",
      "album_context_sha256",
      "source_bindings_sha256",
      "proposal_filename",
      "proposal_file_sha256",
      "proposal_sha256",
      "candidate_sha256",
      "release_mbid",
    ])
    || typeof binding.album_reference !== "string"
    || binding.album_sha256 !== proposal.album.album_sha256
    || binding.album_revision !== proposal.album.album_revision
    || binding.album_context_sha256 !== proposal.album.context_sha256
    || !isDigest(binding.source_bindings_sha256)
    || binding.proposal_filename !== entry.filename
    || binding.proposal_file_sha256 !== entry.file_sha256
    || binding.proposal_sha256 !== proposal.proposal_sha256
    || !isDigest(binding.candidate_sha256)
    || binding.release_mbid !== releaseMbid
  ) {
    throw new Error("The release review is not bound to the exact current candidate.");
  }
  return binding;
}

function validateReleaseReview(review, proposal, entry, releaseMbid) {
  if (
    !hasExactKeys(review, [
      "schema",
      "binding",
      "release",
      "release_sha256",
      "authority",
      "review_sha256",
    ])
    || review.schema !== RELEASE_REVIEW_SCHEMA
    || !isDigest(review.release_sha256)
    || !isDigest(review.review_sha256)
  ) {
    throw new Error("The server returned an invalid MusicBrainz release review.");
  }
  validateReleaseReviewBinding(review.binding, proposal, entry, releaseMbid);
  const release = review.release;
  if (
    !hasExactKeys(release, [
      "release_mbid",
      "title",
      "artist",
      "date",
      "country",
      "status",
      "barcode",
      "label",
      "catalog_number",
      "release_group_mbid",
      "genres",
      "formats",
      "track_count",
      "has_artwork",
      "media",
      "tracklist",
    ])
    || release.release_mbid !== releaseMbid
    || typeof release.title !== "string"
    || !release.title
    || typeof release.artist !== "string"
    || typeof release.date !== "string"
    || typeof release.country !== "string"
    || typeof release.status !== "string"
    || typeof release.barcode !== "string"
    || typeof release.label !== "string"
    || typeof release.catalog_number !== "string"
    || (release.release_group_mbid !== "" && !isMbid(release.release_group_mbid))
    || !Array.isArray(release.genres)
    || release.genres.some((item) => typeof item !== "string")
    || !Array.isArray(release.formats)
    || release.formats.some((item) => typeof item !== "string")
    || !Number.isSafeInteger(release.track_count)
    || release.track_count < 0
    || release.track_count > 2048
    || typeof release.has_artwork !== "boolean"
    || !Array.isArray(release.media)
    || release.media.length > 64
    || !Array.isArray(release.tracklist)
    || release.tracklist.length !== release.track_count
  ) {
    throw new Error("The server returned malformed bounded release facts.");
  }
  for (const medium of release.media) {
    if (
      !hasExactKeys(medium, ["position", "title", "format", "track_count"])
      || !Number.isSafeInteger(medium.position)
      || medium.position < 1
      || typeof medium.title !== "string"
      || typeof medium.format !== "string"
      || !Number.isSafeInteger(medium.track_count)
      || medium.track_count < 0
    ) {
      throw new Error("The server returned malformed release-media facts.");
    }
  }
  for (const track of release.tracklist) {
    if (
      !hasExactKeys(track, [
        "medium_position",
        "medium_title",
        "medium_format",
        "position",
        "number",
        "title",
        "artist",
        "duration_seconds",
      ])
      || !Number.isSafeInteger(track.medium_position)
      || track.medium_position < 1
      || !Number.isSafeInteger(track.position)
      || track.position < 1
      || typeof track.medium_title !== "string"
      || typeof track.medium_format !== "string"
      || typeof track.number !== "string"
      || typeof track.title !== "string"
      || !track.title
      || typeof track.artist !== "string"
      || (track.duration_seconds !== null
        && (!Number.isFinite(track.duration_seconds) || track.duration_seconds < 0))
    ) {
      throw new Error("The server returned a malformed release track list.");
    }
  }
  if (
    !hasExactKeys(review.authority, [
      "read_only",
      "metadata_applied",
      "artwork_downloaded",
      "artwork_applied",
      "may_modify_album_project",
      "may_modify_side_projects",
      "physical_pressing_proven",
      "human_review_required",
    ])
    || review.authority.read_only !== true
    || review.authority.metadata_applied !== false
    || review.authority.artwork_downloaded !== false
    || review.authority.artwork_applied !== false
    || review.authority.may_modify_album_project !== false
    || review.authority.may_modify_side_projects !== false
    || review.authority.physical_pressing_proven !== false
    || review.authority.human_review_required !== true
  ) {
    throw new Error("The MusicBrainz review returned unsupported authority.");
  }
  return review;
}

function validateArtworkReview(review, releaseReview, proposal, entry) {
  if (
    !hasExactKeys(review, ["schema", "binding", "artwork", "authority", "review_sha256"])
    || review.schema !== ARTWORK_REVIEW_SCHEMA
    || !isDigest(review.review_sha256)
  ) {
    throw new Error("The server returned an invalid artwork review.");
  }
  const binding = review.binding;
  const baseBinding = { ...binding };
  delete baseBinding.release_sha256;
  delete baseBinding.release_review_sha256;
  validateReleaseReviewBinding(
    baseBinding,
    proposal,
    entry,
    releaseReview.release.release_mbid,
  );
  if (
    !hasExactKeys(binding, [
      "album_reference",
      "album_sha256",
      "album_revision",
      "album_context_sha256",
      "source_bindings_sha256",
      "proposal_filename",
      "proposal_file_sha256",
      "proposal_sha256",
      "candidate_sha256",
      "release_mbid",
      "release_sha256",
      "release_review_sha256",
    ])
    || binding.release_sha256 !== releaseReview.release_sha256
    || binding.release_review_sha256 !== releaseReview.review_sha256
  ) {
    throw new Error("The artwork review is not bound to the reviewed release facts.");
  }
  const artwork = review.artwork;
  if (
    !hasExactKeys(artwork, [
      "relative_path",
      "source_url",
      "mime_type",
      "sha256",
      "size_bytes",
      "requested_size",
      "selected_size",
      "preview_url",
    ])
    || !/^artwork\/review\/[^/]+\.(?:jpg|png)$/.test(artwork.relative_path)
    || !/^https:\/\/(?:[^/]+\.)?(?:coverartarchive\.org|archive\.org)\//.test(
      artwork.source_url,
    )
    || !["image/jpeg", "image/png"].includes(artwork.mime_type)
    || !isDigest(artwork.sha256)
    || !Number.isSafeInteger(artwork.size_bytes)
    || artwork.size_bytes < 1
    || artwork.size_bytes > 25 * 1024 * 1024
    || artwork.requested_size !== "1200"
    || !["1200", "original"].includes(artwork.selected_size)
    || !/^\/api\/album\/identification\/artwork-preview\/[0-9a-f]{64}$/.test(
      artwork.preview_url,
    )
  ) {
    throw new Error("The server returned invalid hash-bound artwork bytes.");
  }
  if (
    !hasExactKeys(review.authority, [
      "read_only",
      "metadata_applied",
      "artwork_downloaded",
      "artwork_applied",
      "may_modify_album_project",
      "may_modify_side_projects",
      "physical_pressing_proven",
      "human_review_required",
    ])
    || review.authority.read_only !== true
    || review.authority.metadata_applied !== false
    || review.authority.artwork_downloaded !== true
    || review.authority.artwork_applied !== false
    || review.authority.may_modify_album_project !== false
    || review.authority.may_modify_side_projects !== false
    || review.authority.physical_pressing_proven !== false
    || review.authority.human_review_required !== true
  ) {
    throw new Error("The artwork review returned unsupported authority.");
  }
  return review;
}

function validateState(payload) {
  if (
    !hasExactKeys(payload, [
      "schema",
      "album_project",
      "album_project_sha256",
      "album_revision",
      "side_order_policy",
      "metadata",
      "artwork",
      "total_tracks",
      "total_sides",
      "ready_for_export",
      "summary",
      "sides",
      "exceptions",
      "identification",
      "publication",
    ])
    || payload.schema !== WORKBENCH_SCHEMA
  ) {
    throw new Error("The server returned an unsupported Album Workbench state.");
  }
  if (!/^[0-9a-f]{64}$/.test(payload.album_project_sha256)) {
    throw new Error("The album project identity is missing or invalid.");
  }
  if (!Number.isSafeInteger(payload.album_revision) || payload.album_revision <= 0) {
    throw new Error("The album revision is missing or invalid.");
  }
  if (
    !isObject(payload.side_order_policy)
    || payload.side_order_policy.approval_relevant !== true
    || payload.side_order_policy.reorder_invalidates_all_side_pins !== true
    || typeof payload.side_order_policy.reason !== "string"
  ) {
    throw new Error("The server returned an unsupported side-order approval policy.");
  }
  if (!isObject(payload.metadata) || !Array.isArray(payload.sides) || !Array.isArray(payload.exceptions)) {
    throw new Error("The server returned an incomplete Album Workbench state.");
  }
  for (const side of payload.sides) {
    if (!isObject(side) || typeof side.label !== "string" || !Number.isSafeInteger(side.order)) {
      throw new Error("The server returned an invalid album side.");
    }
    validateIdentity(side.current_identity, `Side ${side.label}`);
  }
  if (
    payload.artwork !== null
    && (
      !isObject(payload.artwork)
      || typeof payload.artwork.path !== "string"
      || !/^[0-9a-f]{64}$/.test(payload.artwork.sha256)
    )
  ) {
    throw new Error("The server returned invalid album artwork state.");
  }
  for (const exception of payload.exceptions) {
    if (
      !isObject(exception)
      || typeof exception.id !== "string"
      || typeof exception.type !== "string"
      || !["blocker", "review"].includes(exception.severity)
      || typeof exception.title !== "string"
      || typeof exception.message !== "string"
      || !isObject(exception.evidence)
      || !Array.isArray(exception.actions)
    ) {
      throw new Error("The server returned an invalid album exception.");
    }
  }
  validateIdentificationState(payload.identification, payload.album_project_sha256);
  validatePublicationState(payload.publication, payload.album_project_sha256);
  return payload;
}

function identitiesEqual(left, right) {
  return IDENTITY_FIELDS.every((field) => left[field] === right[field]);
}

function expectedSides(state = albumState) {
  return [...state.sides]
    .sort((left, right) => left.order - right.order)
    .map((side) => ({
      side_label: side.label,
      current_identity: validateIdentity(side.current_identity, `Side ${side.label}`),
    }));
}

function mutationPreconditions(state = albumState) {
  return {
    expected_album_sha256: state.album_project_sha256,
    expected_album_revision: state.album_revision,
    expected_sides: expectedSides(state),
  };
}

function validateOpenSideResponse(payload, sideLabel, expectedIdentity) {
  const fields = ["ok", "url", "side_label", "current_identity", "reused"];
  if (
    !isObject(payload)
    || Object.keys(payload).length !== fields.length
    || fields.some((field) => !Object.hasOwn(payload, field))
    || payload.ok !== true
    || payload.side_label !== sideLabel
    || typeof payload.reused !== "boolean"
  ) {
    throw new Error("The server returned an invalid side-review response.");
  }
  const currentIdentity = validateIdentity(payload.current_identity, `Side ${sideLabel}`);
  if (!identitiesEqual(currentIdentity, expectedIdentity)) {
    throw new Error("The opened side-review identity does not match this workbench state.");
  }
  if (typeof payload.url !== "string") {
    throw new Error("The server returned an invalid side-review URL.");
  }
  let url;
  try {
    url = new URL(payload.url);
  } catch {
    throw new Error("The server returned an invalid side-review URL.");
  }
  if (
    url.protocol !== "http:"
    || !/^groove-serpent-[a-f0-9]{32}\.localhost$/.test(url.hostname)
    || !url.port
    || url.username
    || url.password
    || !/^\/__groove_serpent_session__\/[A-Za-z0-9_-]{43}$/.test(url.pathname)
    || url.search
    || url.hash
  ) {
    throw new Error("The side-review URL is not a safe loopback endpoint.");
  }
  return { url: url.href, reused: payload.reused };
}

async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.headers || {}),
    },
  });
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = { error: `HTTP ${response.status}` };
  }
  if (!response.ok) {
    const error = new Error(asText(payload?.error, `HTTP ${response.status}`));
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function fetchAlbumState(signal) {
  return requestJson("/api/album/state", signal ? { signal } : {});
}

function setNotice(message, kind = "", showReload = false) {
  elements.globalNoticeText.textContent = message;
  elements.globalNotice.className = `global-notice${kind ? ` ${kind}` : ""}`;
  elements.noticeReloadButton.classList.toggle("hidden", !showReload);
}

function setLoading(loading) {
  elements.workbench.setAttribute("aria-busy", String(loading));
  elements.refreshButton.disabled = loading || mutationBusy;
  elements.refreshButton.classList.toggle("loading", loading);
  if (loading) setNotice(albumState ? "Refreshing the exact album state…" : "Loading the latest album state…");
}

function showLoadFailure(error) {
  elements.loadFailureMessage.textContent = error.message;
  elements.loadFailure.classList.remove("hidden");
  elements.workbenchContent.classList.add("hidden");
  elements.retryButton.focus();
}

function clearLoadFailure() {
  elements.loadFailure.classList.add("hidden");
  elements.workbenchContent.classList.remove("hidden");
}

function metadataValue(metadata, ...keys) {
  for (const key of keys) {
    const value = asText(metadata[key]).trim();
    if (value) return value;
  }
  return "";
}

function renderAlbumOverview() {
  const metadata = albumState.metadata;
  const title = metadataValue(metadata, "album", "title");
  const artist = metadataValue(metadata, "album_artist", "artist");
  const counts = exceptionCounts();
  elements.albumTitle.textContent = title || "Untitled album";
  elements.albumArtist.textContent = artist || "Album artist not set";
  elements.albumPath.textContent = asText(albumState.album_project);
  elements.readiness.className = "readiness";
  if (counts.blockers > 0 || !albumState.ready_for_export) {
    elements.readiness.classList.add("blocked");
    elements.readinessValue.textContent = "Blocked";
    elements.readinessNote.textContent = `${plural(counts.blockers, "decision")} must be resolved`;
  } else if (counts.reviews > 0) {
    elements.readiness.classList.add("review");
    elements.readinessValue.textContent = "Ready with reviews";
    elements.readinessNote.textContent = `${plural(counts.reviews, "item")} remains visible`;
  } else {
    elements.readiness.classList.add("ready");
    elements.readinessValue.textContent = "Ready for export";
    elements.readinessNote.textContent = "No unresolved decisions";
  }
}

function setEditorStatus(element, message = "", kind = "") {
  element.textContent = message;
  element.className = `editor-status${kind ? ` ${kind}` : ""}`;
}

function renderPairingEditor() {
  elements.metadataAlbum.value = asText(albumState.metadata.album);
  elements.metadataAlbumArtist.value = asText(albumState.metadata.album_artist);
  elements.metadataArtist.value = asText(albumState.metadata.artist);
  elements.metadataYear.value = asText(albumState.metadata.year);
  elements.metadataGenre.value = asText(albumState.metadata.genre);
  elements.artworkPath.value = asText(albumState.artwork?.path);
  elements.orderPolicyReason.textContent = albumState.side_order_policy.reason;
  elements.orderApprovalCheckbox.checked = false;
  setEditorStatus(elements.detailsStatus);
  setEditorStatus(elements.addSideStatus);
  setEditorStatus(elements.orderStatus);
  updateEditorControls();
}

function selectedPublicationProfiles() {
  return [...elements.publicationProfiles.querySelectorAll('input[type="checkbox"]:checked')]
    .map((input) => input.value);
}

function selectedRestorationMode() {
  return elements.publicationRestorationModes.querySelector('input[type="radio"]:checked')?.value || "";
}

function currentPublicationPlan(planSha256) {
  return albumState?.publication.catalog.entries.find(
    (entry) => entry.status === "current" && entry.plan_sha256 === planSha256,
  ) || null;
}

function portableKey(value) {
  return value.normalize("NFC").toLocaleLowerCase("en-US");
}

function suggestedDestination(base) {
  const occupied = new Set([
    ...albumState.publication.operations.publications.map((item) => portableKey(item.directory_name)),
    ...albumState.publication.operations.orphans.map((item) => portableKey(item.directory_name)),
    ...albumState.publication.catalog.entries.map((item) => portableKey(item.filename)),
  ]);
  if (!occupied.has(portableKey(base))) return base;
  for (let suffix = 2; suffix <= 999; suffix += 1) {
    const candidate = `${base}-${suffix}`;
    if (!occupied.has(portableKey(candidate))) return candidate;
  }
  return "";
}

function updateExactPreview(input, preview) {
  const value = input.value.trim();
  preview.textContent = value;
  preview.title = value;
}

function updateIdentificationControls() {
  if (!albumState) return;
  const canScan = albumState.identification.readiness.can_scan;
  const disabled = identificationBusy || mutationBusy || publicationBusy || staleState;
  elements.identificationNetworkReviewed.disabled = disabled || !canScan;
  elements.identificationScanButton.disabled = disabled
    || !canScan
    || !elements.identificationNetworkReviewed.checked;
  for (const button of elements.identificationCatalog.querySelectorAll("button")) {
    button.disabled = disabled;
  }
  elements.releaseDetailsNetworkReviewed.disabled = disabled
    || !selectedIdentificationProposal;
  for (const button of elements.identificationCandidates.querySelectorAll(
    "button[data-release-details]",
  )) {
    button.disabled = disabled || !elements.releaseDetailsNetworkReviewed.checked;
  }
  elements.copyReleaseMetadataButton.disabled = disabled || !selectedReleaseReview;
  elements.artworkNetworkReviewed.disabled = disabled || !selectedReleaseReview;
  elements.downloadArtworkButton.disabled = disabled
    || !selectedReleaseReview
    || !elements.artworkNetworkReviewed.checked
    || Boolean(selectedArtworkReview);
  elements.closeIdentificationReview.disabled = identificationBusy;
}

function renderIdentificationProvider() {
  const provider = albumState.identification.provider;
  const rows = [
    ["Provider", provider.provider || "Not configured"],
    ["Status", provider.ready ? "Ready for an explicit scan" : "Unavailable"],
    ["Fingerprint backend", provider.fingerprint_backend || "Not available"],
    ["Detail", provider.message],
  ];
  if (provider.missing.length) rows.push(["Missing", provider.missing.join(", ")]);
  const children = [];
  for (const [label, value] of rows) {
    children.push(createElement("dt", "", label), createElement("dd", "", value));
  }
  replaceChildren(elements.identificationProvider, children);
}

function renderIdentificationCatalog() {
  const catalog = albumState.identification.catalog;
  const summary = catalog.summary;
  elements.identificationCatalogSummary.textContent = `${summary.current} current \u00B7 ${summary.stale} stale \u00B7 ${summary.invalid} invalid`;
  if (!catalog.entries.length) {
    replaceChildren(elements.identificationCatalog, [
      createElement(
        "div",
        "publication-plan-empty",
        "No identification proposals were found directly beside this album project.",
      ),
    ]);
    return;
  }
  const cards = catalog.entries.map((entry) => {
    const card = createElement("article", `identification-proposal-card ${entry.status}`);
    const title = createElement("div", "identification-proposal-title");
    title.append(
      createElement("strong", "", entry.filename),
      createElement("span", "identification-badge", entry.status),
    );
    card.append(title);
    if (entry.decision_status) {
      card.append(createElement(
        "p",
        "",
        `${formatLabel(entry.decision_status)} \u00B7 ${formatLabel(entry.confidence || "none")} confidence`,
      ));
    }
    if (entry.selected_release_mbid) {
      card.append(createElement("p", "publication-plan-hash", entry.selected_release_mbid));
    }
    for (const issue of entry.issues) {
      card.append(createElement(
        "p",
        "publication-plan-issue",
        `${formatLabel(issue.code)}: ${issue.message}`,
      ));
    }
    if (entry.selectable && isDigest(entry.file_sha256) && isDigest(entry.proposal_sha256)) {
      const button = createElement("button", "quiet-button", "Review ranked candidates");
      button.type = "button";
      button.addEventListener("click", () => openIdentificationProposal(entry));
      card.append(button);
    }
    return card;
  });
  replaceChildren(elements.identificationCatalog, cards);
}

function candidateFact(label, value) {
  const item = createElement("span");
  item.append(label, createElement("strong", "", value));
  return item;
}

function renderIdentificationReview(proposal = selectedIdentificationProposal) {
  const proposalChanged = Boolean(
    selectedIdentificationProposal
    && proposal
    && selectedIdentificationProposal.proposal_sha256 !== proposal.proposal_sha256,
  );
  if (!proposal || proposalChanged) {
    selectedReleaseReview = null;
    selectedArtworkReview = null;
    reviewedArtworkSelection = null;
    elements.releaseDetailsNetworkReviewed.checked = false;
    elements.artworkNetworkReviewed.checked = false;
    setEditorStatus(elements.releaseDetailsStatus);
    setEditorStatus(elements.artworkReviewStatus);
  }
  selectedIdentificationProposal = proposal;
  if (!proposal) {
    elements.identificationReview.classList.add("hidden");
    replaceChildren(elements.identificationDecision);
    replaceChildren(elements.identificationCandidates);
    renderReleaseDetailsReview(null);
    replaceChildren(elements.identificationPressing);
    return;
  }
  const decision = proposal.decision;
  const evidence = proposal.evidence;
  const selected = decision.selected_release_mbid;
  elements.identificationReview.classList.remove("hidden");
  elements.identificationReviewHeading.textContent = "Ranked release evidence";
  const decisionTitle = createElement(
    "strong",
    "",
    `${formatLabel(decision.status)} \u00B7 ${formatLabel(decision.confidence)} confidence`,
  );
  const decisionCopy = createElement(
    "p",
    "",
    `${evidence.observed_track_count} of ${evidence.album_track_count} tracks supplied bound recognition evidence. ${decision.reasons.map(formatLabel).join(". ")}.`,
  );
  const authorityCopy = createElement(
    "p",
    "",
    "This proposal cannot apply metadata or artwork and does not prove the physical pressing.",
  );
  const factors = proposal.album.sides.map((side) => side.fingerprint_effective_speed_factor);
  const correctedFactors = factors.filter((factor) => Math.abs(factor - 1) > 0.000001);
  const speedCopy = createElement(
    "p",
    "",
    correctedFactors.length
      ? `Fingerprint evidence used the proposal's reviewed pitch-and-tempo speed correction (${correctedFactors.map((factor) => `${factor.toFixed(6)}\u00D7`).join(", ")}); source audio was not modified.`
      : "Fingerprint evidence used the neutral 1.000000\u00D7 pitch-and-tempo factor; source audio was not modified.",
  );
  replaceChildren(
    elements.identificationDecision,
    [decisionTitle, decisionCopy, authorityCopy, speedCopy],
  );

  const candidateCards = proposal.ranked_release_candidates.map((candidate) => {
    const release = candidate.release;
    const card = createElement(
      "article",
      `identification-candidate-card${candidate.release_mbid === selected ? " selected" : ""}`,
    );
    const title = createElement("div", "identification-candidate-title");
    title.append(
      createElement("strong", "", release.title || "Untitled database release"),
      createElement("span", "identification-badge", `Rank ${candidate.rank}`),
    );
    const facts = createElement("div", "identification-candidate-facts");
    facts.append(
      candidateFact("Album coverage", `${(candidate.album_track_coverage * 100).toFixed(1)}%`),
      candidateFact("Mean match", `${(candidate.mean_recognition_score * 100).toFixed(1)}%`),
      candidateFact("Support", `${candidate.supporting_track_count} tracks / ${candidate.supporting_side_count} sides`),
      candidateFact("Country", release.country || "Unknown"),
      candidateFact("Date", release.date || "Unknown"),
      candidateFact("Status", release.status || "Unknown"),
    );
    const fetchButton = createElement(
      "button",
      "quiet-button",
      `Fetch details for ${release.title || candidate.release_mbid}`,
    );
    fetchButton.type = "button";
    fetchButton.dataset.releaseDetails = candidate.release_mbid;
    fetchButton.addEventListener("click", () => fetchCandidateReleaseDetails(candidate));
    card.append(
      title,
      facts,
      createElement("p", "publication-plan-hash", candidate.release_mbid),
      fetchButton,
    );
    return card;
  });
  replaceChildren(elements.identificationCandidates, candidateCards.length ? candidateCards : [
    createElement(
      "div",
      "publication-plan-empty",
      "No exact release candidate survived the bounded consensus rules. Manual description remains available.",
    ),
  ]);
  renderReleaseDetailsReview(selectedReleaseReview);

  const pressing = proposal.exact_pressing_review;
  const heading = createElement("h4", "", "Physical pressing is not proven");
  const missing = pressing.missing_or_unverified_facts.length
    ? `Missing or unverified: ${pressing.missing_or_unverified_facts.map(formatLabel).join(", ")}.`
    : "All listed database facts are present, but they still require physical comparison.";
  const missingCopy = createElement("p", "", missing);
  const checks = createElement("ul", "identification-checks");
  for (const check of pressing.owner_checks_required) {
    checks.append(createElement("li", "", formatLabel(check)));
  }
  const manualCopy = createElement(
    "p",
    "",
    `${pressing.manual_candidates.length} manual review candidate(s); manual candidates never affect automatic ranking.`,
  );
  replaceChildren(elements.identificationPressing, [heading, missingCopy, checks, manualCopy]);
  updateIdentificationControls();
}

function releaseDuration(seconds) {
  if (seconds === null) return "duration unknown";
  const rounded = Math.round(seconds);
  const minutes = Math.floor(rounded / 60);
  return `${minutes}:${String(rounded % 60).padStart(2, "0")}`;
}

function renderArtworkReview(review = selectedArtworkReview) {
  selectedArtworkReview = review;
  if (!review) {
    elements.artworkReview.classList.add("hidden");
    replaceChildren(elements.artworkReview);
    return;
  }
  const artwork = review.artwork;
  const image = createElement("img");
  image.src = artwork.preview_url;
  image.alt = `Downloaded front artwork preview for ${selectedReleaseReview.release.title}`;
  image.width = 1200;
  image.height = 1200;
  const copy = createElement("div", "artwork-review-copy");
  copy.append(
    createElement("strong", "", "Downloaded for review; not applied"),
    createElement("p", "", `Local path: ${artwork.relative_path}`),
    createElement("p", "publication-plan-hash", `SHA-256 ${artwork.sha256}`),
    createElement(
      "p",
      "",
      `${artwork.mime_type} \u00B7 ${artwork.size_bytes.toLocaleString()} bytes \u00B7 no-overwrite file`,
    ),
    createElement(
      "p",
      "",
      "Physical pressing is still not proven. Compare the image with the jacket, labels, catalog number, barcode, and matrix/runout.",
    ),
  );
  const copyPath = createElement(
    "button",
    "quiet-button",
    "Copy reviewed path to manual form (does not save)",
  );
  copyPath.type = "button";
  copyPath.addEventListener("click", () => {
    elements.artworkPath.value = artwork.relative_path;
    reviewedArtworkSelection = {
      path: artwork.relative_path,
      sha256: artwork.sha256,
    };
    setEditorStatus(
      elements.detailsStatus,
      "Reviewed artwork path copied. Save album details remains a separate explicit action.",
      "success",
    );
    updateEditorControls();
  });
  copy.append(copyPath);
  replaceChildren(elements.artworkReview, [image, copy]);
  elements.artworkReview.classList.remove("hidden");
}

function renderReleaseDetailsReview(review = selectedReleaseReview) {
  selectedReleaseReview = review;
  if (!review) {
    elements.releaseDetailsReview.classList.add("hidden");
    replaceChildren(elements.releaseDetailsContent);
    renderArtworkReview(null);
    updateIdentificationControls();
    return;
  }
  const release = review.release;
  elements.releaseDetailsHeading.textContent = `${release.title} \u00B7 reviewed release facts`;
  const facts = createElement("div", "release-details-facts");
  facts.append(
    candidateFact("Artist", release.artist || "Unknown"),
    candidateFact("Date", release.date || "Unknown"),
    candidateFact("Country", release.country || "Unknown"),
    candidateFact("Status", release.status || "Unknown"),
    candidateFact("Label", release.label || "Unknown"),
    candidateFact("Catalog", release.catalog_number || "Unknown"),
    candidateFact("Barcode", release.barcode || "Unknown"),
    candidateFact("Format", release.formats.join(", ") || "Unknown"),
    candidateFact("Tracks", String(release.track_count)),
    candidateFact("Genres", release.genres.join(", ") || "Unspecified"),
    candidateFact("Front artwork", release.has_artwork ? "Reported available" : "Not reported"),
    candidateFact("MusicBrainz ID", release.release_mbid),
  );
  const warning = createElement(
    "p",
    "operation-warning",
    "These are MusicBrainz database facts for one release candidate. They are not physical-pressing proof and were not applied to the album.",
  );
  const tracklist = createElement("ol", "release-tracklist");
  for (const track of release.tracklist) {
    const item = createElement("li");
    item.append(
      createElement("strong", "", `${track.number || track.position}. ${track.title}`),
      ` \u2014 ${track.artist || release.artist || "artist unknown"} (${releaseDuration(track.duration_seconds)})`,
    );
    tracklist.append(item);
  }
  const hash = createElement(
    "p",
    "publication-plan-hash",
    `Exact release-facts SHA-256 ${review.release_sha256} \u00B7 review ${review.review_sha256}`,
  );
  replaceChildren(elements.releaseDetailsContent, [warning, facts, tracklist, hash]);
  elements.releaseDetailsReview.classList.remove("hidden");
  renderArtworkReview(selectedArtworkReview);
  updateIdentificationControls();
}

function renderIdentification() {
  const identification = albumState.identification;
  renderIdentificationProvider();
  renderIdentificationCatalog();
  elements.identificationReadiness.className = "publication-readiness";
  if (identification.readiness.can_scan) {
    elements.identificationReadiness.classList.add("ready");
    elements.identificationReadiness.textContent = "Ready for explicit scan";
  } else {
    elements.identificationReadiness.classList.add("blocked");
    elements.identificationReadiness.textContent = identification.readiness.reason_codes.includes(
      "proposal_catalog_incomplete",
    ) ? "Catalog incomplete" : "Provider unavailable";
  }
  elements.identificationNetworkReviewed.checked = false;
  setEditorStatus(elements.identificationStatus);
  if (
    selectedIdentificationProposal
    && (
      selectedIdentificationProposal.album.album_sha256 !== albumState.album_project_sha256
      || !albumState.identification.catalog.entries.some(
        (entry) => entry.status === "current"
          && entry.proposal_sha256 === selectedIdentificationProposal.proposal_sha256,
      )
    )
  ) {
    selectedIdentificationProposal = null;
    selectedReleaseReview = null;
    selectedArtworkReview = null;
    reviewedArtworkSelection = null;
  }
  renderIdentificationReview();
  updateIdentificationControls();
}

function updatePublicationControls() {
  if (!albumState) return;
  const disabled = publicationBusy || identificationBusy || mutationBusy || staleState;
  for (const form of [
    elements.publicationPlanForm,
    elements.publicationExecutionForm,
    elements.publicationReplayForm,
    elements.publicationRecoveryForm,
  ]) {
    for (const input of form.querySelectorAll("input")) input.disabled = disabled;
  }
  const profiles = selectedPublicationProfiles();
  const mode = selectedRestorationMode();
  const restoredNeedsReview = profiles.includes("restored-side") && mode !== "reviewed";
  const filename = elements.publicationPlanFilename.value.trim();
  elements.publicationPlanFilename.title = filename;
  elements.publicationPlanFilenamePreview.textContent = filename;
  elements.publicationPlanFilenamePreview.title = filename;
  const ready = albumState.publication.readiness.can_create_plan;
  elements.createPublicationPlanButton.disabled = Boolean(
    disabled
    || !ready
    || !profiles.length
    || !mode
    || restoredNeedsReview
    || !filename.endsWith(PUBLICATION_PLAN_SUFFIX)
    || !elements.publicationReviewed.checked
  );
  for (const button of elements.publicationCatalog.querySelectorAll("button")) {
    button.disabled = disabled;
  }
  for (const button of elements.publicationReceipts.querySelectorAll("button")) {
    button.disabled = disabled;
  }
  for (const button of elements.publicationOrphans.querySelectorAll("button")) {
    button.disabled = disabled;
  }

  const selectedPlan = currentPublicationPlan(selectedExecutionPlanSha256);
  elements.publicationExecutionPlan.value = selectedPlan
    ? `${selectedPlan.filename} (${selectedPlan.plan_sha256})`
    : "Choose Execute on a current plan card.";
  elements.publicationExecutionPlan.title = elements.publicationExecutionPlan.value;
  const destination = elements.publicationDestinationName.value.trim();
  const executionPhrase = destination ? `PUBLISH ${destination}` : "PUBLISH destination";
  elements.publicationExecutionPhrase.textContent = executionPhrase;
  updateExactPreview(elements.publicationDestinationName, elements.publicationDestinationPreview);
  elements.executePublicationButton.disabled = Boolean(
    disabled
    || !albumState.publication.readiness.can_execute_current_plan
    || !selectedPlan
    || !destination
    || elements.publicationExecutionConfirmation.value !== executionPhrase
    || !elements.publicationExecutionConfirmed.checked
  );

  const replayReceipt = selectedReplayReceipt
    ? albumState.publication.operations.publications.find(
      (item) => item.status === "current"
        && item.directory_name === selectedReplayReceipt.directory_name
        && item.manifest_sha256 === selectedReplayReceipt.manifest_sha256
        && item.journal_sha256 === selectedReplayReceipt.journal_sha256,
    )
    : null;
  const replayDestination = elements.publicationReplayDestination.value.trim();
  const replayPhrase = replayReceipt && replayDestination
    ? `REPLAY ${replayReceipt.directory_name} TO ${replayDestination}`
    : "REPLAY source TO destination";
  elements.publicationReplayPhrase.textContent = replayPhrase;
  updateExactPreview(elements.publicationReplayDestination, elements.publicationReplayPreview);
  elements.replayPublicationButton.disabled = Boolean(
    disabled
    || !replayReceipt
    || !replayDestination
    || elements.publicationReplayConfirmation.value !== replayPhrase
    || !elements.publicationReplayConfirmed.checked
  );

  const recoveryPhrase = elements.publicationRecoveryPhrase.textContent;
  elements.confirmPublicationRecovery.disabled = Boolean(
    disabled
    || !pendingPublicationRecovery
    || !recoveryPhrase
    || elements.publicationRecoveryConfirmation.value !== recoveryPhrase
    || !elements.publicationRecoveryConfirmed.checked
  );
}

function renderPublicationChoices() {
  const choices = albumState.publication.choices;
  const profileOptions = choices.profiles.map((profile) => {
    const label = createElement("label", "publication-option");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "publication_profile";
    input.value = profile.id;
    label.append(
      input,
      createElement("strong", "", profile.label),
      createElement("small", "", profile.description),
    );
    return label;
  });
  replaceChildren(elements.publicationProfiles, profileOptions);

  const restorationOptions = choices.restoration_modes.map((mode, index) => {
    const label = createElement("label", "publication-option");
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "publication_restoration_mode";
    input.value = mode.id;
    input.checked = index === 0;
    label.append(
      input,
      createElement("strong", "", mode.label),
      createElement("small", "", mode.description),
    );
    return label;
  });
  replaceChildren(elements.publicationRestorationModes, restorationOptions);
  elements.publicationFlacCompression.value = String(choices.flac_compression.default);
  elements.publicationAacBitrate.value = String(choices.aac_bitrate_kbps.default);
  elements.publicationPlanFilename.value = albumState.publication.default_plan_filename;
  elements.publicationReviewed.checked = false;
}

function renderPublicationCatalog() {
  const catalog = albumState.publication.catalog;
  const summary = catalog.summary;
  elements.publicationCatalogSummary.textContent = `${summary.current} current \u00B7 ${summary.stale} stale \u00B7 ${summary.invalid} invalid`;
  if (!catalog.entries.length) {
    replaceChildren(elements.publicationCatalog, [
      createElement(
        "div",
        "publication-plan-empty",
        "No publication plans were found directly beside this album project.",
      ),
    ]);
    return;
  }
  const cards = catalog.entries.map((entry) => {
    const card = createElement("article", `publication-plan-card ${entry.status}`);
    const title = createElement("div", "publication-plan-title");
    title.append(
      createElement("strong", "", entry.filename),
      createElement("span", "publication-plan-badge", entry.status),
    );
    card.append(title);
    if (entry.plan_sha256) {
      card.append(createElement("p", "publication-plan-hash", entry.plan_sha256));
    }
    if (entry.selected_profiles.length) {
      card.append(
        createElement(
          "p",
          "publication-plan-profiles",
          `${entry.selected_profiles.map(formatLabel).join(" \u00B7 ")} \u00B7 ${formatLabel(entry.restoration_mode)}`,
        ),
      );
    }
    for (const issue of entry.issues) {
      card.append(
        createElement(
          "p",
          "publication-plan-issue",
          `${formatLabel(issue.code)}: ${issue.message}`,
        ),
      );
    }
    if (entry.status === "current") {
      const actions = createElement("div", "operation-actions");
      const preflightButton = createElement("button", "quiet-button", "Preflight current plan");
      preflightButton.type = "button";
      preflightButton.dataset.planSha256 = entry.plan_sha256;
      preflightButton.addEventListener("click", () => preflightPublicationPlan(entry.plan_sha256));
      const executeButton = createElement("button", "primary-button", "Execute this plan");
      executeButton.type = "button";
      executeButton.dataset.planSha256 = entry.plan_sha256;
      executeButton.addEventListener("click", () => {
        selectedExecutionPlanSha256 = entry.plan_sha256;
        setEditorStatus(elements.publicationExecutionStatus, `Selected ${entry.filename}.`);
        updatePublicationControls();
        elements.publicationDestinationName.focus();
      });
      actions.append(preflightButton, executeButton);
      card.append(actions);
    }
    return card;
  });
  replaceChildren(elements.publicationCatalog, cards);
}

function renderPublicationOperations() {
  const operations = albumState.publication.operations;
  const summary = operations.summary;
  elements.publicationOperationSummary.textContent = `${summary.current} current \u00B7 ${summary.stale} stale \u00B7 ${summary.orphans} orphaned`;

  const receiptCards = operations.publications.map((receipt) => {
    const card = createElement("article", `publication-receipt-card ${receipt.status}`);
    const title = createElement("div", "publication-receipt-title");
    title.append(
      createElement("strong", "", receipt.directory_name),
      createElement("span", "receipt-badge", receipt.status),
    );
    card.append(title);
    if (receipt.plan_sha256) {
      card.append(createElement("p", "publication-plan-hash", receipt.plan_sha256));
    }
    card.append(createElement("p", "", `${plural(receipt.artifact_count, "verified artifact")}.`));
    for (const issue of receipt.issues) {
      card.append(createElement("p", "publication-plan-issue", `${formatLabel(issue.code)}: ${issue.message}`));
    }
    if (["current", "stale"].includes(receipt.status)) {
      const actions = createElement("div", "operation-actions");
      const verifyButton = createElement("button", "quiet-button", "Verify read-only");
      verifyButton.type = "button";
      verifyButton.addEventListener("click", () => verifyPublicationReceipt(receipt));
      actions.append(verifyButton);
      if (receipt.status === "current") {
        const replayButton = createElement("button", "quiet-button", "Replay to new destination");
        replayButton.type = "button";
        replayButton.addEventListener("click", () => openPublicationReplay(receipt));
        actions.append(replayButton);
      }
      card.append(actions);
    }
    return card;
  });
  replaceChildren(elements.publicationReceipts, receiptCards.length ? receiptCards : [
    createElement("div", "publication-plan-empty", "No final publication receipts were found beside this album."),
  ]);

  const orphanCards = operations.orphans.map((orphan) => {
    const card = createElement("article", `publication-orphan-card${orphan.actionable ? "" : " unsafe"}`);
    const title = createElement("div", "publication-orphan-title");
    title.append(
      createElement("strong", "", orphan.directory_name),
      createElement("span", "receipt-badge", orphan.actionable ? orphan.kind : "not actionable"),
    );
    card.append(title);
    const state = orphan.state ? formatLabel(orphan.state) : "Unverified ownership";
    card.append(createElement("p", "", `${state} \u00B7 ${plural(orphan.file_count, "file")} \u00B7 ${orphan.total_size_bytes.toLocaleString()} bytes`));
    if (orphan.issue) card.append(createElement("p", "publication-plan-issue", orphan.issue));
    if (orphan.actionable) {
      const actions = createElement("div", "operation-actions");
      const quarantine = createElement("button", "primary-button", "Quarantine exact orphan");
      quarantine.type = "button";
      quarantine.addEventListener("click", () => openPublicationRecovery(orphan, "quarantine"));
      const remove = createElement("button", "danger-button", "Remove exact orphan");
      remove.type = "button";
      remove.addEventListener("click", () => openPublicationRecovery(orphan, "remove"));
      actions.append(quarantine, remove);
      card.append(actions);
    }
    return card;
  });
  replaceChildren(elements.publicationOrphans, orphanCards.length ? orphanCards : [
    createElement("div", "publication-plan-empty", "No recognized incomplete or quarantined publication operations were found."),
  ]);
}

function renderPublication() {
  const readiness = albumState.publication.readiness;
  elements.publicationReadiness.className = "publication-readiness";
  if (readiness.can_create_plan) {
    elements.publicationReadiness.classList.add("ready");
    elements.publicationReadiness.textContent = "Ready for reviewed planning";
  } else {
    elements.publicationReadiness.classList.add("blocked");
    elements.publicationReadiness.textContent = readiness.reason_codes.includes("plan_catalog_incomplete")
      ? "Catalog incomplete"
      : "Resolve album blockers first";
  }
  renderPublicationChoices();
  renderPublicationCatalog();
  renderPublicationOperations();
  if (!currentPublicationPlan(selectedExecutionPlanSha256)) selectedExecutionPlanSha256 = null;
  elements.publicationDestinationName.value = suggestedDestination(
    albumState.publication.default_destination_name,
  );
  elements.publicationExecutionConfirmation.value = "";
  elements.publicationExecutionConfirmed.checked = false;
  if (selectedReplayReceipt && !albumState.publication.operations.publications.some(
    (item) => item.status === "current"
      && item.directory_name === selectedReplayReceipt.directory_name
      && item.manifest_sha256 === selectedReplayReceipt.manifest_sha256,
  )) {
    closePublicationReplay();
  }
  setEditorStatus(elements.publicationPlanStatus);
  setEditorStatus(elements.publicationExecutionStatus);
  setEditorStatus(elements.publicationRecoveryStatus);
  updatePublicationControls();
}

function exceptionsForSide(sideLabel) {
  return albumState.exceptions.filter((item) => item.side_label === sideLabel);
}

function formatSpeed(side) {
  const factor = Number(side.effective_speed_factor);
  if (!Number.isFinite(factor)) return "Speed unavailable";
  const delta = (factor - 1) * 100;
  const sign = delta > 0 ? "+" : "";
  return `${factor.toFixed(6)}× (${sign}${delta.toFixed(3)}%)`;
}

function selectSide(side) {
  const candidates = exceptionsForSide(side.label);
  if (!candidates.length) return;
  selectedExceptionId = candidates[0].id;
  renderExceptionQueue();
  renderExceptionDetail();
  elements.detailHeading.focus({ preventScroll: true });
  elements.exceptionDetail.scrollIntoView({ behavior: "smooth", block: "start" });
}

function updateSideOpenControls() {
  for (const [sideLabel, button] of sideOpenButtons) {
    const opening = openingSides.has(sideLabel);
    button.disabled = opening || identificationBusy || publicationBusy || mutationBusy || staleState;
    button.setAttribute("aria-busy", String(opening));
    button.textContent = opening ? "Opening exact review…" : "Open exact side review";
  }
}

function setSideOpenStatus(sideLabel, kind, message, fallbackUrl = "") {
  const status = sideOpenStatuses.get(sideLabel);
  if (!status) return;
  status.className = `side-open-status${kind ? ` ${kind}` : ""}`;
  status.replaceChildren(document.createTextNode(message));
  if (fallbackUrl) {
    status.append(document.createTextNode(" "));
    const link = createElement("a", "side-open-link", `Open Side ${sideLabel} review`);
    link.href = fallbackUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    status.append(link);
  }
}

function prepareSidePopup(sideLabel) {
  let popup = null;
  try {
    popup = window.open("", "_blank");
    if (popup) {
      popup.opener = null;
      popup.document.title = `Opening Side ${sideLabel} review`;
      popup.document.body.textContent = `Groove Serpent is opening the exact Side ${sideLabel} review…`;
    }
  } catch {
    closePreparedPopup(popup);
    popup = null;
  }
  return popup;
}

function closePreparedPopup(popup) {
  try {
    if (popup && !popup.closed) popup.close();
  } catch {
    // A browser may revoke the blank window proxy; no cleanup action remains.
  }
}

function navigatePreparedPopup(popup, url) {
  if (!popup) return false;
  try {
    popup.location.replace(url);
    return true;
  } catch {
    closePreparedPopup(popup);
    return false;
  }
}

function postOpenSide(payload) {
  return requestJson("/api/album/open-side", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function openSideReview(side) {
  if (openingSides.has(side.label)) return;
  if (staleState) {
    setSideOpenStatus(
      side.label,
      "error",
      "Reload the album state before opening this side.",
    );
    return;
  }

  let expectedIdentity;
  try {
    expectedIdentity = validateIdentity(side.current_identity, `Side ${side.label}`);
  } catch (error) {
    setSideOpenStatus(side.label, "error", error.message);
    return;
  }
  const expectedAlbumSha256 = albumState.album_project_sha256;
  const expectedAlbumRevision = albumState.album_revision;
  const popup = prepareSidePopup(side.label);
  openingSides.add(side.label);
  updateSideOpenControls();
  setSideOpenStatus(
    side.label,
    "busy",
    `Opening Side ${side.label} against its exact current identity…`,
  );

  try {
    const payload = await postOpenSide({
      expected_album_sha256: expectedAlbumSha256,
      expected_album_revision: expectedAlbumRevision,
      side_label: side.label,
      expected_current_identity: expectedIdentity,
    });
    const result = validateOpenSideResponse(payload, side.label, expectedIdentity);
    const liveSide = albumState.sides.find((item) => item.label === side.label);
    if (
      albumState.album_project_sha256 !== expectedAlbumSha256
      || albumState.album_revision !== expectedAlbumRevision
      || !liveSide
      || !identitiesEqual(
        validateIdentity(liveSide.current_identity, `Side ${side.label}`),
        expectedIdentity,
      )
    ) {
      closePreparedPopup(popup);
      throw new Error("The workbench refreshed while this side was opening. Open it again from the current state.");
    }

    const opened = navigatePreparedPopup(popup, result.url);
    if (opened) {
      setSideOpenStatus(
        side.label,
        "success",
        `${result.reused ? "Reused" : "Opened"} the exact Side ${side.label} review in a new tab. Opening is not approval.`,
      );
    } else {
      setSideOpenStatus(
        side.label,
        "warning",
        "The browser blocked the new tab. Use this verified local link instead:",
        result.url,
      );
    }
  } catch (error) {
    closePreparedPopup(popup);
    if (error.status === 409) {
      staleState = true;
      setNotice(
        "This album or side changed after it was loaded. Reload the current state before opening or approving a side.",
        "stale",
        true,
      );
      setSideOpenStatus(
        side.label,
        "error",
        "This side changed. Reload, review its new identity, then open it again.",
      );
      renderExceptionDetail();
      updateSideOpenControls();
    } else {
      setSideOpenStatus(side.label, "error", `Could not open Side ${side.label}: ${error.message}`);
    }
  } finally {
    openingSides.delete(side.label);
    updateSideOpenControls();
  }
}

function renderSides() {
  const sides = [...albumState.sides].sort((left, right) => left.order - right.order);
  const counts = exceptionCounts();
  const totalTracks = asCount(albumState.total_tracks, sides.reduce((sum, side) => sum + asCount(side.tracks), 0));
  elements.sideSummary.textContent = `${plural(sides.length, "side")} · ${plural(totalTracks, "track")} · ${plural(counts.blockers, "blocker")}`;
  sideOpenButtons.clear();
  sideOpenStatuses.clear();
  sideOrderButtons.clear();
  sideRemoveButtons.clear();
  if (!sides.length) {
    const empty = createElement("p", "queue-empty", "No album sides are present in this project.");
    replaceChildren(elements.sideCards, [empty]);
    return;
  }
  const cards = sides.map((side, index) => {
    const sideExceptions = exceptionsForSide(side.label);
    const blockers = sideExceptions.filter((item) => item.severity === "blocker").length;
    const reviews = sideExceptions.filter((item) => item.severity === "review").length;
    const interactive = sideExceptions.length > 0;
    const card = createElement("article", "side-card");
    card.classList.add(blockers ? "blocked" : "ready");
    const titleId = `album-side-title-${index + 1}`;
    const statusId = `album-side-open-status-${index + 1}`;
    card.setAttribute("aria-labelledby", titleId);

    const letter = createElement("span", "side-letter", asText(side.label, "?"));
    letter.setAttribute("aria-hidden", "true");
    const body = createElement("div", "side-body");
    const title = createElement("strong", "", `Side ${side.label}`);
    title.id = titleId;
    body.append(title);
    const stats = createElement("div", "side-stats");
    stats.append(
      createElement("span", "", plural(asCount(side.tracks), "track")),
      createElement("span", "", formatSpeed(side)),
      createElement("span", "", `${formatLabel(asText(side.speed_mode, "unknown"))} speed`),
    );
    body.append(stats);
    const source = asText(side.source) || asText(side.project);
    if (source) body.append(createElement("p", "side-source", source));

    let stateText = "READY";
    let stateClass = "";
    if (blockers) {
      stateText = plural(blockers, "BLOCKER").toUpperCase();
      stateClass = "blocker";
    } else if (reviews) {
      stateText = plural(reviews, "REVIEW").toUpperCase();
      stateClass = "review";
    } else if (!side.pinned) {
      stateText = "UNPINNED";
      stateClass = "blocker";
    }
    const stateLabel = createElement("span", `side-state ${stateClass}`.trim(), stateText);
    const actions = createElement("div", "side-actions");
    if (interactive) {
      const reviewButton = createElement("button", "side-review-button", `Review ${plural(sideExceptions.length, "decision")}`);
      reviewButton.type = "button";
      reviewButton.setAttribute(
        "aria-label",
        `Review ${plural(sideExceptions.length, `Side ${side.label} decision`)}`,
      );
      reviewButton.addEventListener("click", () => selectSide(side));
      actions.append(reviewButton);
    }
    const openButton = createElement("button", "open-side-button", "Open exact side review");
    openButton.type = "button";
    openButton.setAttribute("aria-label", `Open exact Side ${side.label} review`);
    openButton.setAttribute("aria-describedby", statusId);
    openButton.addEventListener("click", () => openSideReview(side));
    sideOpenButtons.set(side.label, openButton);
    actions.append(openButton);

    const earlierButton = createElement("button", "side-order-button", "Move earlier");
    earlierButton.type = "button";
    earlierButton.dataset.available = String(index > 0);
    earlierButton.setAttribute("aria-label", `Move Side ${side.label} earlier`);
    earlierButton.addEventListener("click", () => reorderSide(side.label, -1));
    const laterButton = createElement("button", "side-order-button", "Move later");
    laterButton.type = "button";
    laterButton.dataset.available = String(index < sides.length - 1);
    laterButton.setAttribute("aria-label", `Move Side ${side.label} later`);
    laterButton.addEventListener("click", () => reorderSide(side.label, 1));
    sideOrderButtons.set(side.label, [earlierButton, laterButton]);
    actions.append(earlierButton, laterButton);

    const removeButton = createElement("button", "side-remove-button", "Remove side");
    removeButton.type = "button";
    removeButton.setAttribute("aria-label", `Remove Side ${side.label} from this album`);
    removeButton.addEventListener("click", () => openRemoveSideDialog(side));
    sideRemoveButtons.set(side.label, removeButton);
    actions.append(removeButton);

    const openStatus = createElement("p", "side-open-status");
    openStatus.id = statusId;
    openStatus.setAttribute("role", "status");
    openStatus.setAttribute("aria-live", "polite");
    sideOpenStatuses.set(side.label, openStatus);
    card.append(letter, body, stateLabel, actions, openStatus);
    return card;
  });
  replaceChildren(elements.sideCards, cards);
  updateSideOpenControls();
  updateEditorControls();
}

function findSelectedException() {
  return albumState?.exceptions.find((item) => item.id === selectedExceptionId) || null;
}

function selectException(exceptionId, options = {}) {
  selectedExceptionId = exceptionId;
  renderExceptionQueue();
  renderExceptionDetail();
  if (options.focusDetail !== false) elements.detailHeading.focus({ preventScroll: true });
}

function renderExceptionQueue() {
  const counts = exceptionCounts();
  elements.attentionTotal.textContent = String(counts.total);
  elements.attentionTotal.setAttribute("aria-label", plural(counts.total, "exception"));
  elements.blockerCount.textContent = String(counts.blockers);
  elements.reviewCount.textContent = String(counts.reviews);
  elements.queueHelp.textContent = counts.total
    ? "Choose an exception to see the exact evidence and available action. Use Up and Down Arrow keys to move through the queue."
    : "There are no unresolved album decisions in the current state.";

  if (!albumState.exceptions.length) {
    const empty = createElement("div", "queue-empty");
    empty.append(
      createElement("strong", "", "Queue clear"),
      createElement("p", "", "Every current album decision is resolved. Refresh after changing a side project."),
    );
    replaceChildren(elements.exceptionQueue, [empty]);
    return;
  }

  const buttons = albumState.exceptions.map((exception) => {
    const button = createElement("button", `exception-item ${exception.severity}`);
    button.type = "button";
    button.dataset.exceptionId = exception.id;
    button.setAttribute("aria-current", String(exception.id === selectedExceptionId));
    button.setAttribute("aria-label", `${formatLabel(exception.severity)}: ${exception.title}`);
    const marker = createElement("span", "exception-mark");
    marker.setAttribute("aria-hidden", "true");
    const copy = createElement("span", "exception-copy");
    copy.append(
      createElement("strong", "", exception.title),
      createElement("small", "", exception.side_label ? `Side ${exception.side_label} · ${formatLabel(exception.type)}` : `Album · ${formatLabel(exception.type)}`),
    );
    const arrow = createElement("span", "exception-arrow", "›");
    arrow.setAttribute("aria-hidden", "true");
    button.append(marker, copy, arrow);
    button.addEventListener("click", () => selectException(exception.id));
    return button;
  });
  replaceChildren(elements.exceptionQueue, buttons);
}

function appendFact(label, value) {
  const group = document.createElement("div");
  group.append(createElement("dt", "", label), createElement("dd", "", value));
  elements.detailFacts.append(group);
}

function appendValue(container, value) {
  if (isObject(value)) {
    const list = createElement("dl", "identity-list");
    for (const [key, item] of Object.entries(value)) {
      const row = document.createElement("div");
      row.append(
        createElement("dt", "", formatLabel(key)),
        createElement("dd", "", item === null ? "Not recorded" : String(item)),
      );
      list.append(row);
    }
    container.append(list);
    return;
  }
  const rendered = value === null || value === undefined || value === "" ? "Not recorded" : String(value);
  container.append(createElement("code", "", rendered));
}

function renderEvidence(exception) {
  const entries = Object.entries(exception.evidence);
  if (!entries.length) {
    const card = createElement("div", "evidence-card wide");
    card.append(createElement("strong", "", "Evidence"), createElement("code", "", "No comparison values were supplied."));
    replaceChildren(elements.evidenceGrid, [card]);
    return;
  }
  const cards = entries.map(([key, value]) => {
    const card = createElement("div", `evidence-card${isObject(value) ? " wide" : ""}`);
    card.append(createElement("strong", "", formatLabel(key)));
    appendValue(card, value);
    return card;
  });

  const side = albumState.sides.find((item) => item.label === exception.side_label);
  if (side && !entries.some(([key]) => key === "current" && isObject(exception.evidence.current))) {
    const identityCard = createElement("div", "evidence-card wide");
    identityCard.append(createElement("strong", "", "Exact Current Side Identity"));
    appendValue(identityCard, side.current_identity);
    cards.push(identityCard);
  }
  replaceChildren(elements.evidenceGrid, cards);
}

function canRepin(exception) {
  return Boolean(
    exception
    && exception.severity === "blocker"
    && exception.side_label
    && exception.actions.includes("repin_side")
    && albumState.sides.some((side) => side.label === exception.side_label),
  );
}

function guidanceFor(exception) {
  const guidance = exception.actions
    .map((action) => ACTION_GUIDANCE[action])
    .filter(Boolean);
  return guidance.join(" ") || "Review this decision in the relevant album or side project, then refresh this workbench.";
}

function renderExceptionDetail() {
  const exception = findSelectedException();
  elements.reviewedCheckbox.checked = false;
  elements.repinButton.disabled = true;
  elements.repinStatus.textContent = "";
  elements.repinStatus.className = "repin-status";

  if (!exception) {
    elements.detailContent.classList.add("hidden");
    elements.detailEmpty.classList.remove("hidden");
    if (albumState?.exceptions.length) {
      elements.detailEmptyHeading.textContent = "Choose a decision to review";
      elements.detailEmptyCopy.textContent = "Select an item in Needs Attention to inspect its exact evidence.";
    } else {
      elements.detailEmptyHeading.textContent = "No unresolved decisions";
      elements.detailEmptyCopy.textContent = "The current album state has no blockers or review-only exceptions.";
    }
    return;
  }

  elements.detailEmpty.classList.add("hidden");
  elements.detailContent.classList.remove("hidden");
  elements.detailContext.textContent = exception.side_label ? `SIDE ${exception.side_label} REVIEW` : "ALBUM REVIEW";
  elements.detailHeading.textContent = exception.title;
  elements.detailSeverity.textContent = exception.severity;
  elements.detailSeverity.className = `severity-badge ${exception.severity}`;
  elements.detailMessage.textContent = exception.message;
  elements.detailFacts.replaceChildren();
  appendFact("Type", formatLabel(exception.type));
  appendFact("Scope", exception.side_label ? `Side ${exception.side_label}` : "Whole album");
  appendFact("Field", exception.field ? formatLabel(exception.field) : "Identity set");
  renderEvidence(exception);

  const repinAvailable = canRepin(exception);
  elements.repinPanel.classList.toggle("hidden", !repinAvailable);
  elements.manualActionPanel.classList.toggle("hidden", repinAvailable);
  elements.manualActionCopy.textContent = guidanceFor(exception);
  elements.reviewedCheckbox.disabled = staleState || mutationBusy;
  if (staleState && repinAvailable) {
    elements.repinStatus.textContent = "Reload the current album state before approving a new pin.";
    elements.repinStatus.className = "repin-status error";
  }
}

function renderState(options = {}) {
  const preserveId = options.preserveSelection ? selectedExceptionId : null;
  if (preserveId && albumState.exceptions.some((item) => item.id === preserveId)) {
    selectedExceptionId = preserveId;
  } else {
    selectedExceptionId = albumState.exceptions[0]?.id || null;
  }
  renderAlbumOverview();
  renderPairingEditor();
  renderSides();
  renderIdentification();
  renderPublication();
  renderExceptionQueue();
  renderExceptionDetail();
}

async function loadState(options = {}) {
  const generation = ++loadGeneration;
  if (loadController) loadController.abort();
  loadController = new AbortController();
  setLoading(true);
  try {
    const payload = validateState(await fetchAlbumState(loadController.signal));
    if (generation !== loadGeneration) return;
    albumState = payload;
    staleState = false;
    clearLoadFailure();
    renderState({ preserveSelection: options.preserveSelection !== false });
    const counts = exceptionCounts();
    setNotice(
      counts.total
        ? `State verified · ${plural(counts.blockers, "blocker")} · ${plural(counts.reviews, "review")}`
        : "State verified · the exception queue is clear",
      "success",
    );
    if (options.focusHeading) elements.attentionHeading.focus();
  } catch (error) {
    if (error.name === "AbortError" || generation !== loadGeneration) return;
    if (!albumState) showLoadFailure(error);
    else setNotice(`Refresh failed: ${error.message}`, "error", true);
  } finally {
    if (generation === loadGeneration) {
      setLoading(false);
      loadController = null;
    }
  }
}

function postAlbumMutation(path, payload) {
  return requestJson(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function updateEditorControls() {
  const disabled = mutationBusy || identificationBusy || publicationBusy || staleState || !albumState;
  for (const control of elements.pairingEditor.querySelectorAll("input, button")) {
    control.disabled = disabled;
  }
  elements.saveDetailsButton.disabled = disabled;
  elements.addSideButton.disabled = disabled;
  elements.orderApprovalCheckbox.disabled = disabled;
  const orderApproved = !disabled && elements.orderApprovalCheckbox.checked;
  for (const buttons of sideOrderButtons.values()) {
    for (const button of buttons) {
      button.disabled = !orderApproved || button.dataset.available !== "true";
    }
  }
  for (const button of sideRemoveButtons.values()) {
    button.disabled = disabled || albumState.sides.length <= 1;
  }
  const phrase = pendingRemoveSide ? `REMOVE ${pendingRemoveSide.label}` : "";
  elements.removeSideConfirmation.disabled = disabled;
  elements.confirmRemoveSide.disabled = disabled
    || !pendingRemoveSide
    || elements.removeSideConfirmation.value !== phrase;
  elements.cancelRemoveSide.disabled = mutationBusy;
}

async function applyAlbumMutation(path, payload, options) {
  const beforeState = albumState;
  let accepted = false;
  setMutationBusy(true);
  setEditorStatus(options.statusElement, options.busyMessage, "busy");
  setNotice(options.busyMessage);
  try {
    const rawResponseState = await postAlbumMutation(path, payload);
    accepted = true;
    const responseState = validateState(rawResponseState);
    if (
      responseState.album_revision !== beforeState.album_revision + 1
      || responseState.album_project_sha256 === beforeState.album_project_sha256
    ) {
      throw new Error("The server did not return one exact newly saved album revision.");
    }
    const refreshedState = validateState(await fetchAlbumState());
    if (
      refreshedState.album_revision !== responseState.album_revision
      || refreshedState.album_project_sha256 !== responseState.album_project_sha256
    ) {
      throw new Error("The saved album could not be reproduced from the current file.");
    }
    albumState = refreshedState;
    staleState = false;
    renderState({ preserveSelection: true });
    if (typeof options.afterSuccess === "function") options.afterSuccess();
    setEditorStatus(options.statusElement, options.successMessage, "success");
    setNotice(options.successMessage, "success");
    return true;
  } catch (error) {
    if (error.status === 409 || accepted) {
      staleState = true;
      const message = accepted
        ? "The edit was accepted, but its saved state could not be verified. Reload before any other action."
        : "The album, a side, its source, or its artwork changed. Reload before editing.";
      setEditorStatus(options.statusElement, message, "error");
      setNotice(message, "stale", true);
      renderExceptionDetail();
      updateSideOpenControls();
    } else {
      const message = `Edit failed: ${error.message}`;
      setEditorStatus(options.statusElement, message, "error");
      setNotice(message, "error");
    }
    return false;
  } finally {
    setMutationBusy(false);
  }
}

async function saveAlbumDetails() {
  if (!albumState || mutationBusy || staleState) return;
  const metadata = { ...albumState.metadata };
  const controls = {
    album: elements.metadataAlbum,
    album_artist: elements.metadataAlbumArtist,
    artist: elements.metadataArtist,
    year: elements.metadataYear,
    genre: elements.metadataGenre,
  };
  for (const field of EDITABLE_METADATA_FIELDS) metadata[field] = controls[field].value;
  const artworkPath = elements.artworkPath.value.trim();
  const expectedArtworkSha256 = reviewedArtworkSelection?.path === artworkPath
    ? reviewedArtworkSelection.sha256
    : (albumState.artwork?.path === artworkPath ? albumState.artwork.sha256 : null);
  await applyAlbumMutation(
    "/api/album/update-details",
    {
      ...mutationPreconditions(),
      metadata,
      artwork_path: artworkPath || null,
      expected_artwork_sha256: artworkPath ? expectedArtworkSha256 : null,
    },
    {
      statusElement: elements.detailsStatus,
      busyMessage: "Saving exact album metadata and artwork…",
      successMessage: "Album details saved and reproduced from disk.",
      afterSuccess: () => {
        reviewedArtworkSelection = null;
      },
    },
  );
}

async function addAlbumSide() {
  if (!albumState || mutationBusy || staleState) return;
  const sideLabel = elements.newSideLabel.value.trim();
  const projectReference = elements.newSideProject.value.trim();
  if (!sideLabel || !projectReference) {
    setEditorStatus(
      elements.addSideStatus,
      "Enter both a side label and contained project path.",
      "error",
    );
    return;
  }
  await applyAlbumMutation(
    "/api/album/add-side",
    {
      ...mutationPreconditions(),
      side_label: sideLabel,
      project_reference: projectReference,
    },
    {
      statusElement: elements.addSideStatus,
      busyMessage: `Verifying and adding Side ${sideLabel} as unpinned…`,
      successMessage: `Side ${sideLabel} added unpinned. Review it before repinning.`,
      afterSuccess: () => {
        elements.newSideLabel.value = "";
        elements.newSideProject.value = "";
      },
    },
  );
}

async function reorderSide(sideLabel, delta) {
  if (!albumState || mutationBusy || staleState) return;
  if (!elements.orderApprovalCheckbox.checked) {
    setEditorStatus(
      elements.orderStatus,
      "Acknowledge the pin-clearing policy before changing order.",
      "error",
    );
    elements.orderApprovalCheckbox.focus();
    return;
  }
  const labels = [...albumState.sides]
    .sort((left, right) => left.order - right.order)
    .map((side) => side.label);
  const current = labels.indexOf(sideLabel);
  const target = current + delta;
  if (current < 0 || target < 0 || target >= labels.length) return;
  [labels[current], labels[target]] = [labels[target], labels[current]];
  await applyAlbumMutation(
    "/api/album/reorder-sides",
    {
      ...mutationPreconditions(),
      ordered_side_labels: labels,
      approval_acknowledged: true,
    },
    {
      statusElement: elements.orderStatus,
      busyMessage: `Moving Side ${sideLabel} and clearing every side pin…`,
      successMessage: "Side order saved. Every side is now unpinned for review.",
    },
  );
}

function openRemoveSideDialog(side) {
  if (!albumState || mutationBusy || staleState || albumState.sides.length <= 1) return;
  pendingRemoveSide = side;
  const phrase = `REMOVE ${side.label}`;
  elements.removeSideHeading.textContent = `Remove Side ${side.label}`;
  elements.removeSidePhrase.textContent = phrase;
  elements.removeSideCopy.textContent = `Side ${side.label} will be removed from this album project only. Its project and source audio remain untouched.`;
  elements.removeSideConfirmation.value = "";
  setEditorStatus(elements.removeSideStatus);
  updateEditorControls();
  elements.removeSideDialog.showModal();
  elements.removeSideConfirmation.focus();
}

function closeRemoveSideDialog() {
  if (mutationBusy) return;
  if (elements.removeSideDialog.open) elements.removeSideDialog.close();
  pendingRemoveSide = null;
  elements.removeSideConfirmation.value = "";
  setEditorStatus(elements.removeSideStatus);
  updateEditorControls();
}

async function removeAlbumSide() {
  if (!pendingRemoveSide || !albumState || mutationBusy || staleState) return;
  const sideLabel = pendingRemoveSide.label;
  const confirmation = elements.removeSideConfirmation.value;
  if (confirmation !== `REMOVE ${sideLabel}`) return;
  const removed = await applyAlbumMutation(
    "/api/album/remove-side",
    {
      ...mutationPreconditions(),
      side_label: sideLabel,
      confirmation,
    },
    {
      statusElement: elements.removeSideStatus,
      busyMessage: `Removing Side ${sideLabel} and clearing remaining pins…`,
      successMessage: `Side ${sideLabel} removed. Remaining sides are unpinned.`,
    },
  );
  if (removed) closeRemoveSideDialog();
}

function currentIdentificationEntry(state, expected) {
  return state.identification.catalog.entries.find(
    (entry) => entry.status === "current"
      && entry.filename === expected.filename
      && entry.file_sha256 === expected.file_sha256
      && entry.proposal_sha256 === expected.proposal_sha256,
  ) || null;
}

function setIdentificationBusy(busy) {
  identificationBusy = busy;
  elements.identificationScanForm.setAttribute("aria-busy", String(busy));
  updateIdentificationControls();
  updateEditorControls();
  updateSideOpenControls();
  updatePublicationControls();
}

function validateIdentificationScan(scan) {
  if (
    !hasExactKeys(scan, [
      "album_context_sha256",
      "album_track_count",
      "matched_track_count",
      "unmatched_track_count",
      "total_match_count",
    ])
    || !isDigest(scan.album_context_sha256)
    || !Number.isSafeInteger(scan.album_track_count)
    || scan.album_track_count < 1
    || !Number.isSafeInteger(scan.matched_track_count)
    || scan.matched_track_count < 0
    || !Number.isSafeInteger(scan.unmatched_track_count)
    || scan.unmatched_track_count < 0
    || scan.matched_track_count + scan.unmatched_track_count !== scan.album_track_count
    || !Number.isSafeInteger(scan.total_match_count)
    || scan.total_match_count < scan.matched_track_count
  ) {
    throw new Error("The server returned an invalid identification-scan receipt.");
  }
  return scan;
}

async function scanAlbumIdentification() {
  if (
    !albumState
    || mutationBusy
    || publicationBusy
    || identificationBusy
    || staleState
    || !albumState.identification.readiness.can_scan
    || !elements.identificationNetworkReviewed.checked
  ) return;
  const before = albumState;
  let accepted = false;
  setIdentificationBusy(true);
  setEditorStatus(
    elements.identificationStatus,
    "Fingerprinting exact immutable track snapshots and querying the reviewed provider...",
    "busy",
  );
  setNotice("Release identification is running. Source audio remains local.");
  try {
    const response = await postAlbumMutation(
      "/api/album/identification/scan",
      {
        ...mutationPreconditions(before),
        action: "scan-current-track-fingerprints",
        network_reviewed: true,
      },
    );
    accepted = true;
    if (
      !hasExactKeys(response, [
        "ok",
        "completion",
        "network_request_performed",
        "provider",
        "scan",
        "proposal",
        "catalog_entry",
        "state",
      ])
      || response.ok !== true
      || !["proposal-created", "proposal-reused", "abstained-no-matches"].includes(
        response.completion,
      )
      || response.network_request_performed !== true
    ) {
      throw new Error("The server returned an invalid identification response.");
    }
    validateIdentificationScan(response.scan);
    const responseState = validateState(response.state);
    if (
      responseState.album_project_sha256 !== before.album_project_sha256
      || responseState.album_revision !== before.album_revision
      || JSON.stringify(response.provider) !== JSON.stringify(responseState.identification.provider)
    ) {
      throw new Error("The identification receipt does not match the exact album state.");
    }

    let proposal = null;
    let catalogEntry = null;
    if (response.completion === "abstained-no-matches") {
      if (
        response.proposal !== null
        || response.catalog_entry !== null
        || response.scan.matched_track_count !== 0
        || response.scan.total_match_count !== 0
      ) {
        throw new Error("A no-match scan returned unsupported proposal authority.");
      }
    } else {
      proposal = validateIdentificationProposal(
        response.proposal,
        before.album_project_sha256,
      );
      catalogEntry = response.catalog_entry;
      const currentEntry = isObject(catalogEntry)
        ? currentIdentificationEntry(responseState, catalogEntry)
        : null;
      if (
        !currentEntry
        || catalogEntry.proposal_sha256 !== proposal.proposal_sha256
        || JSON.stringify(catalogEntry) !== JSON.stringify(currentEntry)
      ) {
        throw new Error("The identification proposal was not reopened as current.");
      }
    }

    const refreshed = validateState(await fetchAlbumState());
    if (
      refreshed.album_project_sha256 !== before.album_project_sha256
      || refreshed.album_revision !== before.album_revision
      || (catalogEntry && !currentIdentificationEntry(refreshed, catalogEntry))
    ) {
      throw new Error("The identification result did not survive disk rediscovery.");
    }
    albumState = refreshed;
    staleState = false;
    selectedReleaseReview = null;
    selectedArtworkReview = null;
    reviewedArtworkSelection = null;
    selectedIdentificationProposal = proposal;
    renderState({ preserveSelection: true });
    renderIdentificationReview(proposal);
    if (proposal) {
      setEditorStatus(
        elements.identificationStatus,
        `${formatLabel(response.completion)}: ${response.scan.matched_track_count} of ${response.scan.album_track_count} tracks matched.`,
        "success",
      );
      setNotice(
        "The immutable release proposal was verified and reopened. Human pressing review is still required.",
        "success",
      );
      elements.identificationReview.scrollIntoView({ block: "start" });
    } else {
      setEditorStatus(
        elements.identificationStatus,
        `No candidates: all ${response.scan.album_track_count} track fingerprints abstained.`,
        "error",
      );
      setNotice(
        "Recognition found no bounded release evidence. Nothing was changed; manual description remains available.",
        "success",
      );
    }
  } catch (error) {
    if (error.status === 409 || accepted) {
      staleState = true;
      const message = accepted
        ? "The scan completed, but its exact reopened state could not be verified. Reload before continuing."
        : "The album, a side, or a source changed before identification completed. Reload.";
      setEditorStatus(elements.identificationStatus, message, "error");
      setNotice(message, "stale", true);
    } else {
      const message = `Identification did not complete: ${error.message}`;
      setEditorStatus(elements.identificationStatus, message, "error");
      setNotice(message, "error");
    }
  } finally {
    setIdentificationBusy(false);
  }
}

async function openIdentificationProposal(entry) {
  if (
    !albumState
    || mutationBusy
    || publicationBusy
    || identificationBusy
    || staleState
    || !entry.selectable
    || !isDigest(entry.file_sha256)
    || !isDigest(entry.proposal_sha256)
  ) return;
  const before = albumState;
  let accepted = false;
  setIdentificationBusy(true);
  setEditorStatus(elements.identificationStatus, `Opening ${entry.filename} read-only...`, "busy");
  try {
    const response = await postAlbumMutation(
      "/api/album/identification/open-proposal",
      {
        ...mutationPreconditions(before),
        action: "open-current-identification-proposal",
        filename: entry.filename,
        file_sha256: entry.file_sha256,
        proposal_sha256: entry.proposal_sha256,
      },
    );
    accepted = true;
    if (
      !hasExactKeys(response, ["ok", "read_only", "proposal", "catalog_entry", "state"])
      || response.ok !== true
      || response.read_only !== true
    ) {
      throw new Error("The server returned an invalid read-only proposal response.");
    }
    const proposal = validateIdentificationProposal(
      response.proposal,
      before.album_project_sha256,
    );
    const responseState = validateState(response.state);
    const responseEntry = isObject(response.catalog_entry)
      ? currentIdentificationEntry(responseState, response.catalog_entry)
      : null;
    if (
      responseState.album_project_sha256 !== before.album_project_sha256
      || responseState.album_revision !== before.album_revision
      || !responseEntry
      || response.catalog_entry.filename !== entry.filename
      || response.catalog_entry.file_sha256 !== entry.file_sha256
      || response.catalog_entry.proposal_sha256 !== proposal.proposal_sha256
      || JSON.stringify(response.catalog_entry) !== JSON.stringify(responseEntry)
    ) {
      throw new Error("The opened proposal is no longer the selected current proposal.");
    }
    const refreshed = validateState(await fetchAlbumState());
    if (!currentIdentificationEntry(refreshed, response.catalog_entry)) {
      throw new Error("The proposal did not remain current after independent rediscovery.");
    }
    albumState = refreshed;
    selectedReleaseReview = null;
    selectedArtworkReview = null;
    reviewedArtworkSelection = null;
    selectedIdentificationProposal = proposal;
    renderState({ preserveSelection: true });
    renderIdentificationReview(proposal);
    setEditorStatus(elements.identificationStatus, `Opened ${entry.filename} read-only.`, "success");
    setNotice("Ranked release evidence reopened from the exact immutable proposal.", "success");
    elements.identificationReview.scrollIntoView({ block: "start" });
  } catch (error) {
    if (error.status === 409 || accepted) {
      staleState = true;
      const message = "The identification proposal or a bound album identity changed. Reload.";
      setEditorStatus(elements.identificationStatus, message, "error");
      setNotice(message, "stale", true);
    } else {
      const message = `Proposal could not be opened: ${error.message}`;
      setEditorStatus(elements.identificationStatus, message, "error");
      setNotice(message, "error");
    }
  } finally {
    setIdentificationBusy(false);
  }
}

function selectedCurrentIdentificationEntry() {
  if (!albumState || !selectedIdentificationProposal) return null;
  return albumState.identification.catalog.entries.find(
    (entry) => entry.status === "current"
      && entry.selectable
      && entry.proposal_sha256 === selectedIdentificationProposal.proposal_sha256
      && isDigest(entry.file_sha256),
  ) || null;
}

async function fetchCandidateReleaseDetails(candidate) {
  const entry = selectedCurrentIdentificationEntry();
  if (
    !albumState
    || !selectedIdentificationProposal
    || !entry
    || identificationBusy
    || mutationBusy
    || publicationBusy
    || staleState
    || !elements.releaseDetailsNetworkReviewed.checked
  ) return;
  const before = albumState;
  const proposal = selectedIdentificationProposal;
  setIdentificationBusy(true);
  setEditorStatus(
    elements.releaseDetailsStatus,
    `Fetching exact MusicBrainz facts for ${candidate.release_mbid}...`,
    "busy",
  );
  try {
    const response = await postAlbumMutation(
      "/api/album/identification/release-details",
      {
        ...mutationPreconditions(before),
        action: "fetch-current-candidate-release-details",
        network_reviewed: true,
        proposal_filename: entry.filename,
        proposal_file_sha256: entry.file_sha256,
        proposal_sha256: proposal.proposal_sha256,
        release_mbid: candidate.release_mbid,
      },
    );
    if (
      !hasExactKeys(response, ["ok", "network_request_performed", "review", "state"])
      || response.ok !== true
      || response.network_request_performed !== true
    ) {
      throw new Error("The server returned an invalid release-details receipt.");
    }
    const responseState = validateState(response.state);
    const responseEntry = currentIdentificationEntry(responseState, entry);
    if (
      responseState.album_project_sha256 !== before.album_project_sha256
      || responseState.album_revision !== before.album_revision
      || !responseEntry
    ) {
      throw new Error("The exact album or proposal changed during release lookup.");
    }
    const review = validateReleaseReview(
      response.review,
      proposal,
      responseEntry,
      candidate.release_mbid,
    );
    const refreshed = validateState(await fetchAlbumState());
    if (!currentIdentificationEntry(refreshed, entry)) {
      throw new Error("The release candidate did not remain current after lookup.");
    }
    albumState = refreshed;
    selectedReleaseReview = review;
    selectedArtworkReview = null;
    reviewedArtworkSelection = null;
    elements.releaseDetailsNetworkReviewed.checked = false;
    elements.artworkNetworkReviewed.checked = false;
    renderState({ preserveSelection: true });
    renderReleaseDetailsReview(review);
    setEditorStatus(
      elements.releaseDetailsStatus,
      `Reviewed ${review.release.title}; nothing was applied.`,
      "success",
    );
    setNotice(
      "Exact candidate facts loaded read-only. The physical pressing is still not proven.",
      "success",
    );
    elements.releaseDetailsHeading.focus();
  } catch (error) {
    if (error.status === 409) {
      staleState = true;
      const message = "The exact album, side, source, or proposal changed. Reload.";
      setEditorStatus(elements.releaseDetailsStatus, message, "error");
      setNotice(message, "stale", true);
    } else {
      setEditorStatus(
        elements.releaseDetailsStatus,
        `Release details were not accepted: ${error.message}`,
        "error",
      );
    }
  } finally {
    setIdentificationBusy(false);
  }
}

async function downloadCandidateArtwork() {
  const entry = selectedCurrentIdentificationEntry();
  if (
    !albumState
    || !selectedIdentificationProposal
    || !selectedReleaseReview
    || selectedArtworkReview
    || !entry
    || identificationBusy
    || mutationBusy
    || publicationBusy
    || staleState
    || !elements.artworkNetworkReviewed.checked
  ) return;
  const before = albumState;
  const proposal = selectedIdentificationProposal;
  const releaseReview = selectedReleaseReview;
  setIdentificationBusy(true);
  setEditorStatus(
    elements.artworkReviewStatus,
    "Downloading one no-overwrite front-cover file for review...",
    "busy",
  );
  try {
    const response = await postAlbumMutation(
      "/api/album/identification/download-artwork",
      {
        ...mutationPreconditions(before),
        action: "download-reviewed-candidate-front-artwork",
        network_reviewed: true,
        proposal_filename: entry.filename,
        proposal_file_sha256: entry.file_sha256,
        proposal_sha256: proposal.proposal_sha256,
        release_mbid: releaseReview.release.release_mbid,
        expected_release_review_sha256: releaseReview.review_sha256,
      },
    );
    if (
      !hasExactKeys(response, ["ok", "network_request_performed", "artwork", "state"])
      || response.ok !== true
      || response.network_request_performed !== true
    ) {
      throw new Error("The server returned an invalid artwork-download receipt.");
    }
    const responseState = validateState(response.state);
    const responseEntry = currentIdentificationEntry(responseState, entry);
    if (
      responseState.album_project_sha256 !== before.album_project_sha256
      || responseState.album_revision !== before.album_revision
      || !responseEntry
    ) {
      throw new Error("The exact album or proposal changed during artwork download.");
    }
    const artworkReview = validateArtworkReview(
      response.artwork,
      releaseReview,
      proposal,
      responseEntry,
    );
    selectedArtworkReview = artworkReview;
    elements.artworkNetworkReviewed.checked = false;
    renderArtworkReview(artworkReview);
    setEditorStatus(
      elements.artworkReviewStatus,
      "Artwork downloaded and hash-verified for review; it was not applied.",
      "success",
    );
    setNotice(
      "A new no-overwrite review image was saved locally. Album metadata remains unchanged.",
      "success",
    );
  } catch (error) {
    if (error.status === 409) {
      staleState = true;
      const message = "The exact album, side, source, or proposal changed. Reload.";
      setEditorStatus(elements.artworkReviewStatus, message, "error");
      setNotice(message, "stale", true);
    } else {
      setEditorStatus(
        elements.artworkReviewStatus,
        `Artwork was not accepted: ${error.message}`,
        "error",
      );
    }
  } finally {
    setIdentificationBusy(false);
  }
}

function copyReleaseMetadataToManualForm() {
  if (!selectedReleaseReview || mutationBusy || identificationBusy || staleState) return;
  const release = selectedReleaseReview.release;
  elements.metadataAlbum.value = release.title;
  elements.metadataAlbumArtist.value = release.artist;
  elements.metadataArtist.value = release.artist;
  elements.metadataYear.value = release.date.slice(0, 4);
  elements.metadataGenre.value = release.genres.join("; ");
  setEditorStatus(
    elements.detailsStatus,
    "Reviewed release facts copied into the manual form. Nothing is saved until you choose Save album details.",
    "success",
  );
  updateEditorControls();
}

function setPublicationBusy(busy) {
  publicationBusy = busy;
  elements.publicationPlanForm.setAttribute("aria-busy", String(busy));
  elements.publicationExecutionForm.setAttribute("aria-busy", String(busy));
  elements.publicationReplayForm.setAttribute("aria-busy", String(busy));
  elements.publicationRecoveryForm.setAttribute("aria-busy", String(busy));
  updateIdentificationControls();
  updateEditorControls();
  updateSideOpenControls();
  updatePublicationControls();
}

async function createPublicationPlan() {
  if (!albumState || mutationBusy || publicationBusy || staleState) return;
  const selectedProfiles = selectedPublicationProfiles();
  const restorationMode = selectedRestorationMode();
  const planFilename = elements.publicationPlanFilename.value.trim();
  const flacCompression = Number(elements.publicationFlacCompression.value);
  const aacBitrate = Number(elements.publicationAacBitrate.value);
  if (
    !elements.publicationReviewed.checked
    || !selectedProfiles.length
    || !restorationMode
    || !planFilename.endsWith(PUBLICATION_PLAN_SUFFIX)
    || !Number.isSafeInteger(flacCompression)
    || !Number.isSafeInteger(aacBitrate)
  ) {
    setEditorStatus(
      elements.publicationPlanStatus,
      "Review every choice and supply valid bounded settings before creating a plan.",
      "error",
    );
    return;
  }
  const before = albumState;
  let accepted = false;
  setPublicationBusy(true);
  setEditorStatus(
    elements.publicationPlanStatus,
    "Binding and verifying the immutable publication plan...",
    "busy",
  );
  setNotice("Creating one reviewed immutable publication plan...");
  try {
    const response = await postAlbumMutation(
      "/api/album/publication/create-plan",
      {
        ...mutationPreconditions(before),
        action: "create-reviewed-publication-plan",
        reviewed: true,
        plan_filename: planFilename,
        selected_profiles: selectedProfiles,
        restoration_mode: restorationMode,
        flac_compression: flacCompression,
        aac_bitrate_kbps: aacBitrate,
      },
    );
    accepted = true;
    if (!hasExactKeys(response, ["ok", "created_plan", "state"]) || response.ok !== true) {
      throw new Error("The server returned an invalid create-plan receipt.");
    }
    if (
      !hasExactKeys(response.created_plan, ["filename", "plan_sha256", "selected_profiles", "restoration_mode"])
      || response.created_plan.filename !== planFilename
      || !isDigest(response.created_plan.plan_sha256)
      || !Array.isArray(response.created_plan.selected_profiles)
      || !["none", "reviewed"].includes(response.created_plan.restoration_mode)
    ) {
      throw new Error("The server returned an invalid publication-plan identity.");
    }
    const responseState = validateState(response.state);
    if (
      responseState.album_project_sha256 !== before.album_project_sha256
      || responseState.album_revision !== before.album_revision
      || !responseState.publication.catalog.entries.some(
        (entry) => entry.status === "current"
          && entry.plan_sha256 === response.created_plan.plan_sha256
          && entry.filename === planFilename,
      )
    ) {
      throw new Error("The new plan was not reproduced as current beside this album.");
    }
    const refreshed = validateState(await fetchAlbumState());
    if (
      refreshed.album_project_sha256 !== before.album_project_sha256
      || refreshed.album_revision !== before.album_revision
      || !refreshed.publication.catalog.entries.some(
        (entry) => entry.status === "current"
          && entry.plan_sha256 === response.created_plan.plan_sha256,
      )
    ) {
      throw new Error("The new plan did not survive an independent catalog reopen.");
    }
    albumState = refreshed;
    staleState = false;
    renderState({ preserveSelection: true });
    setEditorStatus(
      elements.publicationPlanStatus,
      `Created and reopened ${planFilename}.`,
      "success",
    );
    setNotice(
      "Publication plan created, strictly preflighted, and rediscovered from disk. No audio was exported.",
      "success",
    );
  } catch (error) {
    if (error.status === 409 || accepted) {
      staleState = true;
      const message = accepted
        ? "The plan was created, but its reopened identity could not be verified. Reload before continuing."
        : "The album, a side, restoration evidence, or tool identity changed. Reload before planning.";
      setEditorStatus(elements.publicationPlanStatus, message, "error");
      setNotice(message, "stale", true);
    } else {
      const message = `Plan creation failed: ${error.message}`;
      setEditorStatus(elements.publicationPlanStatus, message, "error");
      setNotice(message, "error");
    }
  } finally {
    setPublicationBusy(false);
  }
}

async function preflightPublicationPlan(planSha256) {
  if (!albumState || mutationBusy || publicationBusy || staleState || !isDigest(planSha256)) return;
  const before = albumState;
  setPublicationBusy(true);
  setEditorStatus(
    elements.publicationPlanStatus,
    "Revalidating the selected current plan and every bound live input...",
    "busy",
  );
  try {
    const response = await postAlbumMutation(
      "/api/album/publication/preflight",
      {
        ...mutationPreconditions(before),
        action: "preflight-current-publication-plan",
        plan_sha256: planSha256,
      },
    );
    if (
      !hasExactKeys(response, ["ok", "preflight", "state"])
      || response.ok !== true
      || !hasExactKeys(response.preflight, ["filename", "plan_sha256", "album_sha256", "selected_profiles", "side_count"])
      || response.preflight.plan_sha256 !== planSha256
      || response.preflight.album_sha256 !== before.album_project_sha256
      || !Array.isArray(response.preflight.selected_profiles)
      || !Number.isSafeInteger(response.preflight.side_count)
    ) {
      throw new Error("The server returned an invalid publication preflight receipt.");
    }
    const state = validateState(response.state);
    if (
      state.album_project_sha256 !== before.album_project_sha256
      || state.album_revision !== before.album_revision
      || !state.publication.catalog.entries.some(
        (entry) => entry.status === "current" && entry.plan_sha256 === planSha256,
      )
    ) {
      throw new Error("The preflighted plan was not reopened as current.");
    }
    albumState = state;
    renderState({ preserveSelection: true });
    setEditorStatus(
      elements.publicationPlanStatus,
      `Preflight passed for ${response.preflight.filename}.`,
      "success",
    );
    setNotice(
      "Publication preflight passed against the exact current plan and live bindings. No output was created.",
      "success",
    );
  } catch (error) {
    if (error.status === 409) {
      staleState = true;
      const message = "The selected plan or a bound live identity changed. Reload before preflighting again.";
      setEditorStatus(elements.publicationPlanStatus, message, "error");
      setNotice(message, "stale", true);
    } else {
      const message = `Preflight failed: ${error.message}`;
      setEditorStatus(elements.publicationPlanStatus, message, "error");
      setNotice(message, "error");
    }
  } finally {
    setPublicationBusy(false);
  }
}

function renderPublicationProgress(messages) {
  replaceChildren(
    elements.publicationProgress,
    Array.isArray(messages)
      ? messages.map((message) => createElement("li", "", asText(message)))
      : [],
  );
}

function validateVerificationReceipt(value) {
  if (
    !hasExactKeys(value, ["publication_directory", "ok", "manifest_sha256", "journal_sha256", "artifact_count", "mismatches"])
    || typeof value.publication_directory !== "string"
    || typeof value.ok !== "boolean"
    || (value.manifest_sha256 !== null && !isDigest(value.manifest_sha256))
    || (value.journal_sha256 !== null && !isDigest(value.journal_sha256))
    || !Number.isSafeInteger(value.artifact_count)
    || value.artifact_count < 0
    || !Array.isArray(value.mismatches)
  ) {
    throw new Error("The server returned an invalid publication verification receipt.");
  }
  for (const mismatch of value.mismatches) {
    if (
      !hasExactKeys(mismatch, ["code", "path", "expected", "current", "message"])
      || typeof mismatch.code !== "string"
      || (mismatch.path !== null && typeof mismatch.path !== "string")
      || typeof mismatch.message !== "string"
    ) {
      throw new Error("The server returned an invalid publication mismatch.");
    }
  }
  return value;
}

async function executePublicationPlan() {
  if (!albumState || mutationBusy || publicationBusy || staleState) return;
  const plan = currentPublicationPlan(selectedExecutionPlanSha256);
  const destinationName = elements.publicationDestinationName.value.trim();
  const confirmation = `PUBLISH ${destinationName}`;
  if (
    !plan
    || !isDigest(plan.file_sha256)
    || !destinationName
    || elements.publicationExecutionConfirmation.value !== confirmation
    || !elements.publicationExecutionConfirmed.checked
  ) return;

  const before = albumState;
  setPublicationBusy(true);
  setEditorStatus(
    elements.publicationExecutionStatus,
    "Running a fresh preflight, executing to a new directory, then verifying every receipt...",
    "busy",
  );
  setNotice("Publication is running locally. No success will be shown before strict verification.");
  renderPublicationProgress([]);
  try {
    const response = await postAlbumMutation(
      "/api/album/publication/execute",
      {
        ...mutationPreconditions(before),
        action: "execute-current-publication-plan",
        owner_confirmed: true,
        confirmation,
        plan_sha256: plan.plan_sha256,
        plan_file_sha256: plan.file_sha256,
        destination_name: destinationName,
      },
    );
    if (
      !hasExactKeys(response, [
        "ok",
        "completion",
        "destination_name",
        "plan_sha256",
        "plan_file_sha256",
        "preflight",
        "execution",
        "verification",
        "restart_rediscovered",
        "progress",
        "state",
      ])
      || typeof response.ok !== "boolean"
      || !["verified", "verification-failed"].includes(response.completion)
      || response.destination_name !== destinationName
      || response.plan_sha256 !== plan.plan_sha256
      || response.plan_file_sha256 !== plan.file_sha256
      || typeof response.restart_rediscovered !== "boolean"
      || !Array.isArray(response.progress)
      || response.progress.some((item) => typeof item !== "string")
      || !hasExactKeys(response.preflight, ["album_sha256", "plan_sha256", "selected_profiles", "side_count"])
      || response.preflight.album_sha256 !== before.album_project_sha256
      || response.preflight.plan_sha256 !== plan.plan_sha256
      || !Array.isArray(response.preflight.selected_profiles)
      || !Number.isSafeInteger(response.preflight.side_count)
      || !hasExactKeys(response.execution, ["plan_sha256", "artifact_count"])
      || response.execution.plan_sha256 !== plan.plan_sha256
      || !Number.isSafeInteger(response.execution.artifact_count)
    ) {
      throw new Error("The server returned an invalid execution receipt.");
    }
    const verification = validateVerificationReceipt(response.verification);
    const responseState = validateState(response.state);
    renderPublicationProgress(response.progress);
    albumState = responseState;
    renderState({ preserveSelection: true });
    renderPublicationProgress(response.progress);
    if (!response.ok || !verification.ok || !response.restart_rediscovered) {
      const message = "A directory was produced, but strict verification or rediscovery did not pass. It is not marked complete.";
      setEditorStatus(elements.publicationExecutionStatus, message, "error");
      setNotice(message, "error", true);
      return;
    }
    const reopened = validateState(await fetchAlbumState());
    const exactReceipt = reopened.publication.operations.publications.find(
      (item) => item.status === "current"
        && item.directory_name === destinationName
        && item.plan_sha256 === plan.plan_sha256
        && item.manifest_sha256 === verification.manifest_sha256
        && item.journal_sha256 === verification.journal_sha256,
    );
    if (!exactReceipt) {
      throw new Error("The verified publication did not survive independent disk rediscovery.");
    }
    albumState = reopened;
    renderState({ preserveSelection: true });
    renderPublicationProgress(response.progress);
    setEditorStatus(
      elements.publicationExecutionStatus,
      `Verified and reopened ${destinationName} with ${verification.artifact_count} artifacts.`,
      "success",
    );
    setNotice("Publication completed only after independent strict verification and disk rediscovery.", "success");
  } catch (error) {
    if (error.status === 409) {
      staleState = true;
      const message = "The album, plan, destination, or bound input changed. Reload before executing again.";
      setEditorStatus(elements.publicationExecutionStatus, message, "error");
      setNotice(message, "stale", true);
    } else {
      const message = `Publication did not complete: ${error.message}`;
      setEditorStatus(elements.publicationExecutionStatus, message, "error");
      setNotice(message, "error");
    }
  } finally {
    setPublicationBusy(false);
  }
}

async function verifyPublicationReceipt(receipt) {
  if (!albumState || mutationBusy || publicationBusy || staleState) return;
  const before = albumState;
  setPublicationBusy(true);
  setEditorStatus(elements.publicationExecutionStatus, `Verifying ${receipt.directory_name} without changing it...`, "busy");
  try {
    const response = await postAlbumMutation(
      "/api/album/publication/verify",
      {
        ...mutationPreconditions(before),
        action: "verify-discovered-publication",
        directory_name: receipt.directory_name,
        manifest_sha256: receipt.manifest_sha256,
        journal_sha256: receipt.journal_sha256,
        plan_sha256: receipt.plan_sha256,
      },
    );
    if (
      !hasExactKeys(response, ["ok", "read_only", "verification", "state"])
      || typeof response.ok !== "boolean"
      || response.read_only !== true
    ) {
      throw new Error("The server returned an invalid read-only verification result.");
    }
    const verification = validateVerificationReceipt(response.verification);
    albumState = validateState(response.state);
    renderState({ preserveSelection: true });
    const message = response.ok && verification.ok
      ? `Read-only verification passed for ${receipt.directory_name}.`
      : `Read-only verification failed for ${receipt.directory_name}; no files were changed.`;
    setEditorStatus(elements.publicationExecutionStatus, message, response.ok ? "success" : "error");
    setNotice(message, response.ok ? "success" : "error");
  } catch (error) {
    if (error.status === 409) {
      staleState = true;
      const message = "The selected publication receipt changed. Reload before verifying again.";
      setEditorStatus(elements.publicationExecutionStatus, message, "error");
      setNotice(message, "stale", true);
    } else {
      setEditorStatus(elements.publicationExecutionStatus, `Verification failed: ${error.message}`, "error");
    }
  } finally {
    setPublicationBusy(false);
  }
}

function openPublicationReplay(receipt) {
  selectedReplayReceipt = { ...receipt };
  elements.publicationReplayForm.classList.remove("hidden");
  elements.publicationReplaySource.value = `${receipt.directory_name} (${receipt.plan_sha256})`;
  elements.publicationReplaySource.title = elements.publicationReplaySource.value;
  elements.publicationReplayDestination.value = suggestedDestination(`${receipt.directory_name}-replay`);
  elements.publicationReplayConfirmation.value = "";
  elements.publicationReplayConfirmed.checked = false;
  setEditorStatus(elements.publicationReplayStatus);
  updatePublicationControls();
  elements.publicationReplayDestination.focus();
}

function closePublicationReplay() {
  selectedReplayReceipt = null;
  elements.publicationReplayForm.classList.add("hidden");
  elements.publicationReplaySource.value = "";
  elements.publicationReplayDestination.value = "";
  elements.publicationReplayConfirmation.value = "";
  elements.publicationReplayConfirmed.checked = false;
  setEditorStatus(elements.publicationReplayStatus);
  if (albumState) updatePublicationControls();
}

async function replayPublicationReceipt() {
  if (!albumState || !selectedReplayReceipt || mutationBusy || publicationBusy || staleState) return;
  const source = selectedReplayReceipt;
  const plan = currentPublicationPlan(source.plan_sha256);
  const destinationName = elements.publicationReplayDestination.value.trim();
  const confirmation = `REPLAY ${source.directory_name} TO ${destinationName}`;
  if (
    !plan
    || plan.file_sha256 !== source.plan_file_sha256
    || elements.publicationReplayConfirmation.value !== confirmation
    || !elements.publicationReplayConfirmed.checked
  ) return;
  const before = albumState;
  setPublicationBusy(true);
  setEditorStatus(elements.publicationReplayStatus, "Replaying to a new directory, verifying, and comparing receipts...", "busy");
  try {
    const response = await postAlbumMutation(
      "/api/album/publication/replay",
      {
        ...mutationPreconditions(before),
        action: "replay-current-publication",
        owner_confirmed: true,
        confirmation,
        plan_sha256: plan.plan_sha256,
        plan_file_sha256: plan.file_sha256,
        source_directory_name: source.directory_name,
        source_manifest_sha256: source.manifest_sha256,
        source_journal_sha256: source.journal_sha256,
        destination_name: destinationName,
      },
    );
    if (
      !hasExactKeys(response, [
        "ok",
        "completion",
        "source_directory_name",
        "destination_name",
        "plan_sha256",
        "plan_file_sha256",
        "replay",
        "verification",
        "restart_rediscovered",
        "progress",
        "state",
      ])
      || typeof response.ok !== "boolean"
      || !["verified-match", "mismatch"].includes(response.completion)
      || response.source_directory_name !== source.directory_name
      || response.destination_name !== destinationName
      || response.plan_sha256 !== plan.plan_sha256
      || response.plan_file_sha256 !== plan.file_sha256
      || typeof response.restart_rediscovered !== "boolean"
      || !Array.isArray(response.progress)
      || !hasExactKeys(response.replay, ["ok", "mismatches"])
      || typeof response.replay.ok !== "boolean"
      || !Array.isArray(response.replay.mismatches)
    ) {
      throw new Error("The server returned an invalid replay receipt.");
    }
    const verification = validateVerificationReceipt(response.verification);
    albumState = validateState(response.state);
    renderState({ preserveSelection: true });
    renderPublicationProgress(response.progress);
    if (!response.ok || !response.replay.ok || !verification.ok || !response.restart_rediscovered) {
      const message = "The replay directory exists, but verification or comparison did not pass. It is not marked as a matching replay.";
      setEditorStatus(elements.publicationExecutionStatus, message, "error");
      setNotice(message, "error", true);
      return;
    }
    const reopened = validateState(await fetchAlbumState());
    const exactReceipt = reopened.publication.operations.publications.find(
      (item) => item.status === "current"
        && item.directory_name === destinationName
        && item.manifest_sha256 === verification.manifest_sha256
        && item.journal_sha256 === verification.journal_sha256,
    );
    if (!exactReceipt) throw new Error("The replay did not survive independent disk rediscovery.");
    albumState = reopened;
    closePublicationReplay();
    renderState({ preserveSelection: true });
    renderPublicationProgress(response.progress);
    setEditorStatus(elements.publicationExecutionStatus, `Replay ${destinationName} verified and matched the original.`, "success");
    setNotice("Replay completed only after verification, comparison, and disk rediscovery.", "success");
  } catch (error) {
    if (error.status === 409) {
      staleState = true;
      const message = "The source receipt, plan, album, or destination changed. Reload before replaying again.";
      setEditorStatus(elements.publicationReplayStatus, message, "error");
      setNotice(message, "stale", true);
    } else {
      setEditorStatus(elements.publicationReplayStatus, `Replay did not complete: ${error.message}`, "error");
    }
  } finally {
    setPublicationBusy(false);
  }
}

function openPublicationRecovery(orphan, action) {
  pendingPublicationRecovery = { orphan: { ...orphan }, action };
  const quarantine = action === "quarantine";
  const phrase = quarantine
    ? `QUARANTINE ${orphan.directory_name}`
    : `REMOVE OWNED ORPHAN ${orphan.directory_name} ${orphan.journal_sha256}`;
  elements.publicationRecoveryDialogHeading.textContent = quarantine
    ? "Quarantine publication orphan"
    : "Remove publication orphan";
  elements.publicationRecoveryCopy.textContent = quarantine
    ? "This atomically moves the exact owned stage to a reserved quarantine name. It does not resume or publish it."
    : "This permanently removes only the exact owned orphan after repeating its full journal and directory identity checks.";
  elements.publicationRecoveryPhrase.textContent = phrase;
  elements.publicationRecoveryConfirmation.value = "";
  elements.publicationRecoveryConfirmed.checked = false;
  elements.confirmPublicationRecovery.textContent = quarantine
    ? "Quarantine exact orphan"
    : "Remove exact orphan";
  elements.confirmPublicationRecovery.className = quarantine ? "primary-button" : "danger-button";
  setEditorStatus(elements.publicationRecoveryDialogStatus);
  updatePublicationControls();
  if (!elements.publicationRecoveryDialog.open) elements.publicationRecoveryDialog.showModal();
  elements.publicationRecoveryConfirmation.focus();
}

function closePublicationRecovery() {
  if (publicationBusy) return;
  if (elements.publicationRecoveryDialog.open) elements.publicationRecoveryDialog.close();
  pendingPublicationRecovery = null;
  elements.publicationRecoveryConfirmation.value = "";
  elements.publicationRecoveryConfirmed.checked = false;
  setEditorStatus(elements.publicationRecoveryDialogStatus);
  if (albumState) updatePublicationControls();
}

async function recoverPublicationOrphan() {
  if (!albumState || !pendingPublicationRecovery || mutationBusy || publicationBusy || staleState) return;
  const { orphan, action } = pendingPublicationRecovery;
  const confirmation = elements.publicationRecoveryPhrase.textContent;
  if (
    elements.publicationRecoveryConfirmation.value !== confirmation
    || !elements.publicationRecoveryConfirmed.checked
  ) return;
  const before = albumState;
  setPublicationBusy(true);
  setEditorStatus(elements.publicationRecoveryDialogStatus, `${formatLabel(action)} in progress...`, "busy");
  try {
    const response = await postAlbumMutation(
      "/api/album/publication/recover",
      {
        ...mutationPreconditions(before),
        action: "recover-owned-publication-orphan",
        owner_confirmed: true,
        confirmation,
        recovery_action: action,
        orphan_directory_name: orphan.directory_name,
        orphan_kind: orphan.kind,
        plan_sha256: orphan.plan_sha256,
        journal_sha256: orphan.journal_sha256,
        directory_identity: orphan.directory_identity,
      },
    );
    if (
      !hasExactKeys(response, ["ok", "recovery", "state"])
      || response.ok !== true
      || !hasExactKeys(response.recovery, ["action", "original_directory_name", "resulting_directory_name", "removed"])
      || response.recovery.action !== action
      || response.recovery.original_directory_name !== orphan.directory_name
      || (response.recovery.resulting_directory_name !== null && typeof response.recovery.resulting_directory_name !== "string")
      || typeof response.recovery.removed !== "boolean"
    ) {
      throw new Error("The server returned an invalid publication recovery receipt.");
    }
    const reopened = validateState(await fetchAlbumState());
    if (reopened.publication.operations.orphans.some((item) => item.directory_name === orphan.directory_name)) {
      throw new Error("The original orphan name remained after recovery rediscovery.");
    }
    albumState = reopened;
    pendingPublicationRecovery = null;
    if (elements.publicationRecoveryDialog.open) elements.publicationRecoveryDialog.close();
    renderState({ preserveSelection: true });
    const message = action === "quarantine"
      ? `Quarantined ${orphan.directory_name}; no resume or publication occurred.`
      : `Removed the exact owned orphan ${orphan.directory_name}.`;
    setEditorStatus(elements.publicationRecoveryStatus, message, "success");
    setNotice(message, "success");
  } catch (error) {
    if (error.status === 409) {
      staleState = true;
      const message = "The orphan journal or directory identity changed. Reload before recovery.";
      setEditorStatus(elements.publicationRecoveryDialogStatus, message, "error");
      setNotice(message, "stale", true);
    } else {
      setEditorStatus(elements.publicationRecoveryDialogStatus, `Recovery failed: ${error.message}`, "error");
    }
  } finally {
    setPublicationBusy(false);
  }
}

function sideBlockerCount(state, sideLabel) {
  return state.exceptions.filter((item) => item.severity === "blocker" && item.side_label === sideLabel).length;
}

function postRepin(payload) {
  return requestJson("/api/album/repin", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function setMutationBusy(busy) {
  mutationBusy = busy;
  elements.refreshButton.disabled = busy;
  elements.reviewedCheckbox.disabled = busy || staleState;
  elements.repinButton.disabled = busy || staleState || !elements.reviewedCheckbox.checked;
  updateEditorControls();
  updateSideOpenControls();
  updateIdentificationControls();
  updatePublicationControls();
}

async function repinSelectedSide() {
  const exception = findSelectedException();
  if (!exception || !canRepin(exception) || !elements.reviewedCheckbox.checked || staleState) return;
  const side = albumState.sides.find((item) => item.label === exception.side_label);
  if (!side) return;

  let currentIdentity;
  try {
    currentIdentity = validateIdentity(side.current_identity, `Side ${side.label}`);
  } catch (error) {
    elements.repinStatus.textContent = error.message;
    elements.repinStatus.className = "repin-status error";
    return;
  }

  const beforeState = albumState;
  const beforeQueue = beforeState.exceptions.length;
  const beforeSideBlockers = sideBlockerCount(beforeState, side.label);
  setMutationBusy(true);
  elements.repinStatus.textContent = `Recording the reviewed identity for Side ${side.label}…`;
  elements.repinStatus.className = "repin-status";
  setNotice(`Repinning reviewed Side ${side.label}…`);

  try {
    const responseState = validateState(await postRepin({
      expected_album_sha256: beforeState.album_project_sha256,
      expected_album_revision: beforeState.album_revision,
      side_label: side.label,
      expected_current_identity: currentIdentity,
      reviewed: true,
    }));
    if (
      responseState.album_project_sha256 === beforeState.album_project_sha256
      || responseState.album_revision !== beforeState.album_revision + 1
    ) {
      throw new Error("The server did not return a newly recorded album identity.");
    }

    const refreshedState = validateState(await fetchAlbumState());
    if (
      refreshedState.album_project_sha256 !== responseState.album_project_sha256
      || refreshedState.album_revision !== responseState.album_revision
    ) {
      throw new Error("The repinned album could not be reproduced from disk.");
    }
    const afterQueue = refreshedState.exceptions.length;
    const afterSideBlockers = sideBlockerCount(refreshedState, side.label);
    albumState = refreshedState;
    staleState = false;
    renderState({ preserveSelection: true });

    if (afterQueue >= beforeQueue || afterSideBlockers >= beforeSideBlockers) {
      setNotice(
        `Side ${side.label} was repinned, but the verified queue did not decrease (${beforeQueue} → ${afterQueue}). Review the remaining evidence before continuing.`,
        "error",
      );
      return;
    }
    setNotice(
      `Side ${side.label} repinned and verified · Needs Attention decreased ${beforeQueue} → ${afterQueue}`,
      "success",
    );
    elements.attentionHeading.focus();
  } catch (error) {
    if (error.status === 409) {
      staleState = true;
      setNotice(
        "This album or side changed after it was loaded. Reload the current state, review the new evidence, and approve again.",
        "stale",
        true,
      );
      renderExceptionDetail();
      updateSideOpenControls();
    } else {
      elements.repinStatus.textContent = `Repin failed: ${error.message}`;
      elements.repinStatus.className = "repin-status error";
      setNotice(`Repin failed: ${error.message}`, "error");
    }
  } finally {
    setMutationBusy(false);
  }
}

elements.refreshButton.addEventListener("click", () => loadState({ preserveSelection: true }));
elements.noticeReloadButton.addEventListener("click", () => loadState({ preserveSelection: false, focusHeading: true }));
elements.retryButton.addEventListener("click", () => loadState({ preserveSelection: false }));
elements.reviewedCheckbox.addEventListener("change", () => {
  elements.repinButton.disabled = !elements.reviewedCheckbox.checked || staleState || mutationBusy;
});
elements.repinForm.addEventListener("submit", (event) => {
  event.preventDefault();
  repinSelectedSide();
});
elements.albumDetailsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  saveAlbumDetails();
});
elements.addSideForm.addEventListener("submit", (event) => {
  event.preventDefault();
  addAlbumSide();
});
elements.identificationScanForm.addEventListener("input", updateIdentificationControls);
elements.identificationScanForm.addEventListener("change", updateIdentificationControls);
elements.identificationScanForm.addEventListener("submit", (event) => {
  event.preventDefault();
  scanAlbumIdentification();
});
elements.releaseDetailsNetworkReviewed.addEventListener(
  "change",
  updateIdentificationControls,
);
elements.artworkNetworkReviewed.addEventListener("change", updateIdentificationControls);
elements.downloadArtworkButton.addEventListener("click", downloadCandidateArtwork);
elements.copyReleaseMetadataButton.addEventListener(
  "click",
  copyReleaseMetadataToManualForm,
);
elements.closeIdentificationReview.addEventListener("click", () => {
  selectedIdentificationProposal = null;
  renderIdentificationReview(null);
  elements.identificationCatalogHeading.focus();
});
elements.publicationPlanForm.addEventListener("input", updatePublicationControls);
elements.publicationPlanForm.addEventListener("change", updatePublicationControls);
elements.publicationPlanForm.addEventListener("submit", (event) => {
  event.preventDefault();
  createPublicationPlan();
});
elements.publicationExecutionForm.addEventListener("input", updatePublicationControls);
elements.publicationExecutionForm.addEventListener("change", updatePublicationControls);
elements.publicationExecutionForm.addEventListener("submit", (event) => {
  event.preventDefault();
  executePublicationPlan();
});
elements.publicationReplayForm.addEventListener("input", updatePublicationControls);
elements.publicationReplayForm.addEventListener("change", updatePublicationControls);
elements.publicationReplayForm.addEventListener("submit", (event) => {
  event.preventDefault();
  replayPublicationReceipt();
});
elements.cancelPublicationReplay.addEventListener("click", closePublicationReplay);
elements.publicationRecoveryForm.addEventListener("input", updatePublicationControls);
elements.publicationRecoveryForm.addEventListener("change", updatePublicationControls);
elements.publicationRecoveryForm.addEventListener("submit", (event) => {
  event.preventDefault();
  recoverPublicationOrphan();
});
elements.cancelPublicationRecovery.addEventListener("click", closePublicationRecovery);
elements.publicationRecoveryDialog.addEventListener("cancel", () => {
  pendingPublicationRecovery = null;
  elements.publicationRecoveryConfirmation.value = "";
  elements.publicationRecoveryConfirmed.checked = false;
});
elements.publicationRecoveryDialog.addEventListener("close", () => {
  if (!publicationBusy) {
    pendingPublicationRecovery = null;
    elements.publicationRecoveryConfirmation.value = "";
    elements.publicationRecoveryConfirmed.checked = false;
    updatePublicationControls();
  }
});
elements.orderApprovalCheckbox.addEventListener("change", () => {
  setEditorStatus(elements.orderStatus);
  updateEditorControls();
});
elements.cancelRemoveSide.addEventListener("click", closeRemoveSideDialog);
elements.removeSideConfirmation.addEventListener("input", updateEditorControls);
elements.removeSideForm.addEventListener("submit", (event) => {
  event.preventDefault();
  removeAlbumSide();
});
elements.removeSideDialog.addEventListener("cancel", () => {
  pendingRemoveSide = null;
  elements.removeSideConfirmation.value = "";
});
elements.removeSideDialog.addEventListener("close", () => {
  if (!mutationBusy) {
    pendingRemoveSide = null;
    elements.removeSideConfirmation.value = "";
    updateEditorControls();
  }
});
elements.exceptionQueue.addEventListener("keydown", (event) => {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  const buttons = [...elements.exceptionQueue.querySelectorAll("button.exception-item")];
  if (!buttons.length) return;
  const current = buttons.indexOf(document.activeElement);
  let next = current;
  if (event.key === "ArrowDown") next = current < 0 ? 0 : (current + 1) % buttons.length;
  if (event.key === "ArrowUp") next = current < 0 ? buttons.length - 1 : (current - 1 + buttons.length) % buttons.length;
  if (event.key === "Home") next = 0;
  if (event.key === "End") next = buttons.length - 1;
  event.preventDefault();
  buttons[next].focus();
});

loadState({ preserveSelection: false });
