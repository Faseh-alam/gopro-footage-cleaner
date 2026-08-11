import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";
import { toast } from "sonner";
import { Logo } from "@/components/wc/logo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/components/auth/AuthProvider";

export const Route = createFileRoute("/login")({
  head: () => ({
    meta: [
      { title: "Sign in — World Context" },
      { name: "description", content: "Employee login for the GoPro footage pipeline." },
    ],
  }),
  component: LoginPage,
});

function LoginPage() {
  const { login, signup } = useAuth();
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    try {
      if (mode === "login") await login(email.trim(), password);
      else await signup(fullName.trim(), email.trim(), password);
    } catch (error: any) {
      toast.error(error?.message || (mode === "login" ? "Login failed" : "Signup failed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-[#12110f] px-4">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 opacity-80"
        style={{
          background:
            "radial-gradient(ellipse 80% 55% at 50% -10%, rgba(185,109,114,0.22), transparent 55%), radial-gradient(ellipse 50% 40% at 90% 80%, rgba(90,120,100,0.12), transparent 50%)",
        }}
      />
      <div className="relative w-full max-w-md">
        <div className="mb-8 flex flex-col items-center text-center">
          <Logo className="size-10" />
          <div className="mt-4 font-mono text-[11px] uppercase tracking-[0.22em] text-[#b96d72]">
            World Context
          </div>
          <h1 className="mt-2 font-[Syne,sans-serif] text-3xl font-bold tracking-tight text-[#f2ebe3]">
            {mode === "login" ? "Sign in" : "Create account"}
          </h1>
          <p className="mt-2 max-w-sm text-sm text-[#a39a8f]">
            Employee access for review station metrics — work hours, SD cards, and footage processed.
          </p>
        </div>

        <form
          onSubmit={submit}
          className="border border-[#2a2723] bg-[#1a1815]/80 p-6 shadow-[0_24px_80px_rgba(0,0,0,0.35)] backdrop-blur"
        >
          {mode === "signup" && (
            <label className="mb-4 block">
              <span className="mb-1.5 block font-mono text-[10px] uppercase tracking-[0.16em] text-[#8a8178]">
                Full name
              </span>
              <Input
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                placeholder="Your name"
                required
                autoComplete="name"
                className="h-10 rounded-sm border-[#2a2723] bg-[#12110f] text-[#f2ebe3]"
              />
            </label>
          )}
          <label className="mb-4 block">
            <span className="mb-1.5 block font-mono text-[10px] uppercase tracking-[0.16em] text-[#8a8178]">
              Email
            </span>
            <Input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@company.com"
              required
              autoComplete="email"
              className="h-10 rounded-sm border-[#2a2723] bg-[#12110f] text-[#f2ebe3]"
            />
          </label>
          <label className="mb-6 block">
            <span className="mb-1.5 block font-mono text-[10px] uppercase tracking-[0.16em] text-[#8a8178]">
              Password
            </span>
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={mode === "signup" ? "At least 6 characters" : "Password"}
              required
              minLength={6}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              className="h-10 rounded-sm border-[#2a2723] bg-[#12110f] text-[#f2ebe3]"
            />
          </label>

          <Button type="submit" variant="accent" className="h-10 w-full" disabled={busy}>
            {busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
          </Button>

          <p className="mt-5 text-center text-sm text-[#8a8178]">
            {mode === "login" ? (
              <>
                New employee?{" "}
                <button
                  type="button"
                  className="text-[#b96d72] underline-offset-4 hover:underline"
                  onClick={() => setMode("signup")}
                >
                  Create an account
                </button>
              </>
            ) : (
              <>
                Already have an account?{" "}
                <button
                  type="button"
                  className="text-[#b96d72] underline-offset-4 hover:underline"
                  onClick={() => setMode("login")}
                >
                  Sign in
                </button>
              </>
            )}
          </p>
        </form>
      </div>
    </div>
  );
}
