import { useCallback, useEffect, useState } from 'react'
import { ApiError, api, type GpuCard, type GpuProcess, type GpuStatus } from '../api'
import { duration } from '../format'

const REFRESH_MS = 5000

function size(mb: number | null): string {
  if (mb === null) return '?'
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`
}

function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

/** One sentence: is the GPU free, and if not, what's mostly using it. */
function verdict(g: GpuStatus): string {
  const card = g.cards[0]
  if (!card) return g.nvidia_smi ? 'nvidia-smi found no GPU.' : 'Curio can’t read the GPU here (no nvidia-smi).'
  const util = card.utilization ?? 0
  const top = g.processes.find((p) => p.kind !== 'graphics') ?? g.processes[0]
  const ollama = top?.app.startsWith('ollama') && g.ollama.length ? `Ollama (${g.ollama.map((m) => m.name).join(', ')})` : ''
  const who = ollama || (top ? `${top.program}${top.app ? ` (${top.app})` : ''}` : '')
  if (util < 20) return who && (top?.memory_mb ?? 0) > 1024 ? `Mostly idle. ${who} holds ${size(top!.memory_mb)} of memory.` : 'Idle.'
  return who ? `In use, ${util}% busy. The biggest user is ${who}, with ${size(top!.memory_mb)}.` : `In use, ${util}% busy.`
}

function Meter({ label, value, max, text }: { label: string; value: number | null; max: number; text: string }) {
  const pct = value === null || !max ? 0 : Math.min(100, Math.max(0, (value / max) * 100))
  return (
    <div className="gpu__meter">
      <div className="gpu__meter-head">
        <span>{label}</span>
        <strong>{text}</strong>
      </div>
      <div className="gpu__track" role="img" aria-label={`${label}: ${text}`}>
        <div className={`gpu__fill${pct >= 80 ? ' gpu__fill--high' : ''}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

function Card({ card }: { card: GpuCard }) {
  const facts = [
    card.temperature_c !== null && `${card.temperature_c} °C`,
    card.power_w !== null && `${Math.round(card.power_w)} W${card.power_limit_w ? ` of ${Math.round(card.power_limit_w)} W` : ''}`,
    card.fan_percent !== null && `fan ${card.fan_percent}%`,
  ].filter(Boolean)
  return (
    <section className="gpu__card" aria-label={`GPU ${card.index}`}>
      <h2 className="gpu__name">{card.name}</h2>
      <Meter label="Busy" value={card.utilization} max={100} text={card.utilization === null ? '?' : `${card.utilization}%`} />
      <Meter
        label="Memory"
        value={card.memory_used_mb}
        max={card.memory_total_mb ?? 0}
        text={`${size(card.memory_used_mb)} of ${size(card.memory_total_mb)}`}
      />
      {facts.length > 0 && <p className="muted gpu__facts">{facts.join(' · ')}</p>}
    </section>
  )
}

const KIND: Record<GpuProcess['kind'], string> = {
  compute: 'compute',
  graphics: 'graphics',
  both: 'compute and graphics',
}

function Programs({ g }: { g: GpuStatus }) {
  if (!g.processes_visible)
    return (
      <p className="hint">
        Curio can’t see the programs from inside its container. Restart it with <code>./curio.sh restart</code>; the GPU
        check (on when an NVIDIA CDI spec exists) now also lets it see the host’s processes. Set{' '}
        <code>CURIO_GPU_PROCESSES=0</code> to keep them hidden.
      </p>
    )
  if (g.processes.length === 0) return <p className="muted">No programs are using the GPU.</p>
  return (
    <ul className="gpu__procs">
      {g.processes.map((p) => (
        <li key={`${p.gpu}-${p.pid}`} className="gpu__proc">
          <div className="gpu__proc-head">
            <strong className="gpu__program">{p.program}</strong>
            {p.app && <span className={p.curio ? 'queue__badge queue__badge--live' : 'queue__badge'}>{p.app}</span>}
          </div>
          <p className="gpu__proc-facts">
            <span>{size(p.memory_mb)}</span>
            <span>{p.utilization === null ? 'not computing now' : `${p.utilization}% busy`}</span>
            <span>{KIND[p.kind]}</span>
            {p.running_s !== null && <span>running {duration(p.running_s)}</span>}
            <span className="muted">pid {p.pid}</span>
          </p>
          {p.command && <code className="gpu__cmd">{p.command}</code>}
        </li>
      ))}
    </ul>
  )
}

function Ollama({ g }: { g: GpuStatus }) {
  if (!g.ollama_reachable) return <p className="muted">Curio couldn’t reach Ollama.</p>
  if (g.ollama.length === 0) return <p className="muted">No model is loaded.</p>
  return (
    <ul className="gpu__procs">
      {g.ollama.map((m) => (
        <li key={m.name} className="gpu__proc">
          <div className="gpu__proc-head">
            <strong className="gpu__program">{m.name}</strong>
            <span className="queue__badge">{m.curio_model ? 'One of Curio’s models' : 'Not one of Curio’s models'}</span>
          </div>
          <p className="gpu__proc-facts">
            <span>
              {size(m.vram_mb)} on the GPU{m.vram_mb < m.size_mb - 1 ? `, ${size(m.size_mb - m.vram_mb)} in RAM` : ''}
            </span>
            {m.expires_at && <span>unloads at {clockTime(m.expires_at)} if nothing uses it</span>}
          </p>
        </li>
      ))}
    </ul>
  )
}

function Curio({ g }: { g: GpuStatus }) {
  if (g.curio_doing.length > 0) return <p>Analyzing {g.curio_doing.join('; ')}.</p>
  return (
    <p>
      {g.curio_busy ? 'Working on a job (notes, a quiz or a course playlist).' : 'Idle.'}{' '}
      {g.curio_last_model_use
        ? `Its last model job ended at ${clockTime(g.curio_last_model_use)}.`
        : 'No model job since it started.'}{' '}
      <span className="muted">Queue: {g.queue_message}</span>
    </p>
  )
}

/** Who is using the GPU right now, refreshed every few seconds while the page is open. */
export function GpuPage() {
  const [gpu, setGpu] = useState<GpuStatus | null>(null)
  const [err, setErr] = useState('')
  const load = useCallback(() => {
    if (document.hidden) return
    api.gpu().then(
      (g) => {
        setGpu(g)
        setErr('')
      },
      (e) => setErr(e instanceof ApiError ? e.message : 'Couldn’t read the GPU.'),
    )
  }, [])
  useEffect(() => {
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    document.addEventListener('visibilitychange', load)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', load)
    }
  }, [load])

  return (
    <main className="page gpu">
      <h1>GPU</h1>
      <p className="muted page__lede">
        What is using the GPU right now: the programs on it, the models Ollama has loaded, and what Curio is doing.
        Updates every 5 seconds.
      </p>
      {err && (
        <p className="form-error" role="alert">
          {err}
        </p>
      )}
      {gpu === null && !err && <p className="muted">Reading the GPU…</p>}
      {gpu && (
        <>
          <p className="gpu__verdict" role="status">
            {verdict(gpu)} <span className="muted">Checked at {new Date(gpu.checked_at).toLocaleTimeString()}.</span>
          </p>
          <div className="gpu__cards">
            {gpu.cards.map((c) => (
              <Card key={c.index} card={c} />
            ))}
          </div>
          <section className="course__section" aria-labelledby="gpu-programs">
            <h2 id="gpu-programs">Programs on the GPU</h2>
            <Programs g={gpu} />
          </section>
          <section className="course__section" aria-labelledby="gpu-ollama">
            <h2 id="gpu-ollama">Ollama models</h2>
            <p className="hint">Ollama runs every model in its own process, so it can’t say which program asked for one.</p>
            <Ollama g={gpu} />
          </section>
          <section className="course__section" aria-labelledby="gpu-curio">
            <h2 id="gpu-curio">Curio</h2>
            <Curio g={gpu} />
          </section>
        </>
      )}
    </main>
  )
}
