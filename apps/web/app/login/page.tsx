import { LoginForm } from "../../components/login/login-form";
import styles from "../../components/login/login.module.css";

export default function LoginPage() {
  return <main className={styles.page}>
    <section className={styles.intro} aria-hidden="true">
      <div className={styles.orbit}><i /><i /><i /><i /><b>我</b></div>
      <p>记住每一个人，<br />和你们之间发生过的事。</p>
    </section>
    <div className={styles.card}>
      <div className={styles.brand}><span>M</span><div><strong>Monica</strong><small>私人关系档案</small></div></div>
      <LoginForm />
    </div>
  </main>;
}
