async function checkHealth() {
  const status = document.querySelector("#status");
  try {
    const response = await fetch("/health");
    if (!response.ok) throw new Error("Health check failed");
    const result = await response.json();
    status.textContent = result.ok ? "Backend connected" : "Backend unavailable";
  } catch {
    status.textContent = "Unable to connect to the backend. Please try again.";
  }
}

checkHealth();
