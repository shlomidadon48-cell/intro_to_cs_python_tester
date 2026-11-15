importScripts("https://cdn.jsdelivr.net/pyodide/v0.24.1/full/pyodide.js");

// אתחול מוקדם של Pyodide מיד עם יצירת ה-Worker לחיסכון בזמן המתנה בלחיצה
const pyodideReadyPromise = loadPyodide();

self.onmessage = async (event) => {
    const { zipBuffer, exerciseId, fileName } = event.data;

    try {
        const pyodide = await pyodideReadyPromise;
        if (exerciseId === "ex5") {
            self.postMessage({ type: 'stdout', text: "טוען ספריית pytest..." });
            await pyodide.loadPackage("pytest");
        }

        pyodide.setStdout({
            batched: (text) => self.postMessage({ type: 'stdout', text })
        });
        pyodide.setStderr({
            batched: (text) => self.postMessage({ type: 'stdout', text })
        });

        // שימוש בשם קובץ קבוע ונקי במערכת הקבצים הווירטואלית למניעת בעיות נתיבים
        const safeZipName = "submission.zip";
        pyodide.FS.writeFile(safeZipName, new Uint8Array(zipBuffer));

        // משיכת קבצי המערכת עם עקיפת מטמון (Cache-Busting)
        const cacheBuster = `?t=${Date.now()}`;
        const [configText, graderText] = await Promise.all([
            fetch(`config.json${cacheBuster}`).then((r) => r.text()),
            fetch(`grader.py${cacheBuster}`).then((r) => r.text())
        ]);

        pyodide.FS.writeFile("config.json", configText);
        pyodide.FS.writeFile("grader.py", graderText);

        if (exerciseId === "ex1") {
            const karelRes = await fetch(`karel.py${cacheBuster}`);
            if (karelRes.ok) {
                pyodide.FS.writeFile("karel.py", await karelRes.text());
            }
        }

        // העברת משתנים בצורה בטוחה דרך globals (ללא שרשור מחרוזות לקוד פייתון)
        pyodide.globals.set("SUBMISSION_EX_ID", exerciseId);
        pyodide.globals.set("SUBMISSION_ORIG_NAME", fileName);
        pyodide.globals.set("SUBMISSION_ZIP_PATH", safeZipName);

        await pyodide.runPythonAsync(`
            import grader
            try:
                config_db = grader.load_config()
                grader.process_submission(
                    SUBMISSION_EX_ID,
                    SUBMISSION_ZIP_PATH,
                    config_db,
                    display_name=SUBMISSION_ORIG_NAME
                )
            except Exception as e:
                print(f"[!] System Error: {e}")
        `);

        self.postMessage({ type: 'done' });
    } catch (err) {
        self.postMessage({
            type: 'stdout',
            text: "\n[!] 💥 קריסה פנימית של הבודק:\n" + String(err)
        });
        self.postMessage({ type: 'done' });
    }
};