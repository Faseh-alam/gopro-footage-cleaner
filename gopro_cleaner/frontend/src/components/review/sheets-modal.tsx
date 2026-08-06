import { Modal } from "@/components/wc/modal";
import { Button } from "@/components/ui/button";
import { Field, FileInput, TextInput } from "@/components/wc/field";
import { Badge } from "@/components/wc/panel";
import type { SheetsIntegration } from "./useSheetsIntegration";

export function SheetsModal({ sheets }: { sheets: SheetsIntegration }) {
  const s = sheets.modalStatusHtml;

  return (
    <Modal open={sheets.modalOpen} onClose={sheets.closeModal} title="Google Sheets" description="Connect a service account and spreadsheet to log card progress.">
      <div className="grid gap-4">
        {s && (
          <div className="flex flex-wrap gap-2">
            <Badge tone={s.hasCreds ? "ok" : "danger"}>{s.hasCreds ? "credentials" : "no credentials"}</Badge>
            <Badge tone={s.hasSheet ? "ok" : "danger"}>{s.hasSheet ? "spreadsheet" : "no spreadsheet"}</Badge>
            {s.connOk !== null && (
              <Badge tone={s.connOk ? "ok" : "danger"}>{s.connOk ? "connected" : s.connError || "error"}</Badge>
            )}
          </div>
        )}

        <form
          className="grid gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            sheets.submitSetup(new FormData(e.currentTarget));
          }}
        >
          <Field label="Service account JSON" htmlFor="sheets-creds">
            <FileInput id="sheets-creds" name="credentials" accept="application/json" />
          </Field>
          <Field label="Spreadsheet ID or URL" htmlFor="sheets-id">
            <TextInput id="sheets-id" name="spreadsheetId" placeholder="1AbC…" />
          </Field>
          <div className="flex items-center gap-2">
            <Button type="submit" variant="accent" size="sm" disabled={sheets.submitting}>
              {sheets.submitting ? "Connecting…" : "Connect"}
            </Button>
            <Button type="button" variant="outline" size="sm" onClick={sheets.closeModal}>
              Cancel
            </Button>
          </div>
        </form>

        {sheets.result && (
          <p className={sheets.result.ok ? "text-xs text-success" : "text-xs text-destructive"}>
            {sheets.result.message}
          </p>
        )}
      </div>
    </Modal>
  );
}
