import NextAuth from "next-auth";
import GitHub from "next-auth/providers/github";

declare module "next-auth" {
  interface Session {
    accessToken?: string;
    githubId?: string;
    /** GitHub login (handle), captured at sign-in. Advisory only downstream (e.g. session_index
     * `user` field on the agent side is not re-verified there) -- never used as an identity key,
     * that's what githubId is for. */
    login?: string;
  }
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: [
    GitHub({
      // Default scope (read:user user:email) can't list private repos, list branches, or read
      // repo contents -- all required for the repo/branch picker and onboarding-detection
      // (architecture plan Section A). "repo" is broad; narrowing this to a GitHub App with
      // per-repo installation tokens is the documented follow-up once this grows past a small
      // internal tool (see the plan's Section A.1 note on this).
      authorization: { params: { scope: "read:user user:email repo" } },
    }),
  ],
  session: { strategy: "jwt" },
  callbacks: {
    async jwt({ token, account, profile }) {
      // Only set on the initial sign-in request (`account`/`profile` are only present then) --
      // exactly once per login, not re-derived on every token refresh.
      if (account) {
        token.accessToken = account.access_token;
        token.githubId = account.providerAccountId;
        token.login = (profile as { login?: string } | undefined)?.login;
      }
      return token;
    },
    async session({ session, token }) {
      session.accessToken = token.accessToken as string | undefined;
      session.githubId = token.githubId as string | undefined;
      session.login = token.login as string | undefined;
      return session;
    },
  },
});
