using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Security.Cryptography;
using System.Threading.Tasks;
using System.Windows.Forms;

internal sealed class OnlineInstaller : Form
{
    private const string Version = "0.3.2";
    private const string BaseUrl = "https://github.com/kingkk469/shengnian/releases/download/v0.3.2-windows/";
    private const string PayloadHash = "0bd9a8f3ebb406ae401d54e0b6ac0b2eee708554870b3c37a15baba2234ffb80";
    private const string AppHash = "__APP_HASH__";
    private static readonly string[] Names = {"Shengnian-0.3.2-Windows-payload.001", "Shengnian-0.3.2-Windows-payload.002"};
    private static readonly string[] Hashes = {"__PART_1_HASH__", "__PART_2_HASH__"};
    private static readonly long[] Sizes = {__PART_1_SIZE__, __PART_2_SIZE__};
    private readonly TextBox folder = new TextBox();
    private readonly Button install = new Button();
    private readonly Button browse = new Button();
    private readonly Label status = new Label();
    private readonly ProgressBar progress = new ProgressBar();
    private readonly CheckBox launch = new CheckBox();
    private bool busy;
    private string installedApp;

    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length == 4 && args[0] == "--offline-install")
        {
            try
            {
                InstallPayload(args[1], args[2], args[3]);
                return 0;
            }
            catch (Exception error)
            {
                File.WriteAllText(args[3] + ".error.txt", error.ToString());
                return 1;
            }
        }
        ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new OnlineInstaller());
        return 0;
    }

    private OnlineInstaller()
    {
        Text = "声年 Windows 免费版 " + Version;
        ClientSize = new Size(590, 275);
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        Font = new Font("Microsoft YaHei UI", 10);
        var title = new Label {Text = "声年 · 说出来，自动整理", Location = new Point(20, 18), AutoSize = true, Font = new Font(Font.FontFamily, 14, FontStyle.Bold)};
        var tip = new Label {Text = "自动从 GitHub 下载完整程序和离线模型，约 2.24 GB。\n无需安装 Python；启动后可填写自己的 DeepSeek API Key。", Location = new Point(20, 56), Size = new Size(555, 48)};
        folder.Text = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Shengnian", Version);
        folder.SetBounds(20, 112, 445, 29);
        folder.ReadOnly = true;
        browse.Text = "选择目录";
        browse.SetBounds(475, 111, 94, 31);
        browse.Click += delegate {
            using (var chooser = new FolderBrowserDialog())
            {
                chooser.Description = "选择保存位置，程序将安装到其下的 Shengnian-0.3.2 文件夹。";
                if (chooser.ShowDialog(this) == DialogResult.OK)
                    folder.Text = Path.Combine(chooser.SelectedPath, "Shengnian-" + Version);
            }
        };
        progress.SetBounds(20, 156, 550, 20);
        status.SetBounds(20, 182, 550, 32);
        status.Text = "建议预留至少 10 GB 空间；安装需要连接 GitHub。";
        launch.Text = "完成后打开声年";
        launch.Checked = true;
        launch.SetBounds(20, 229, 220, 28);
        install.Text = "下载并安装";
        install.SetBounds(445, 224, 125, 36);
        install.Click += async delegate {
            if (installedApp != null) Process.Start(new ProcessStartInfo(installedApp) {UseShellExecute = true});
            else await DownloadAndInstall();
        };
        FormClosing += delegate(object sender, FormClosingEventArgs e) { if (busy) e.Cancel = true; };
        Controls.AddRange(new Control[] {title, tip, folder, browse, progress, status, launch, install});
    }

    private static string Sha256(string path)
    {
        using (var stream = File.OpenRead(path))
        using (var algorithm = SHA256.Create())
            return BitConverter.ToString(algorithm.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }

    private static bool ValidPart(string path, int index)
    {
        return File.Exists(path) && new FileInfo(path).Length == Sizes[index] && Sha256(path) == Hashes[index];
    }

    private static void InstallPayload(string partDirectory, string cache, string destination)
    {
        if (Directory.Exists(destination) && Directory.GetFileSystemEntries(destination).Length != 0)
            throw new IOException("安装目录已有文件，请选择新的空目录。已有程序和个人数据不会被覆盖。");
        Directory.CreateDirectory(cache);
        string payload = Path.Combine(cache, "Shengnian-" + Version + "-payload.exe");
        for (int index = 0; index < Names.Length; index++)
            if (!ValidPart(Path.Combine(partDirectory, Names[index]), index))
                throw new IOException("下载文件校验未通过，请重试：" + Names[index]);
        using (var output = new FileStream(payload, FileMode.Create, FileAccess.Write))
            foreach (string name in Names)
                using (var input = File.OpenRead(Path.Combine(partDirectory, name))) input.CopyTo(output);
        if (Sha256(payload) != PayloadHash) throw new IOException("完整程序校验未通过。");
        Directory.CreateDirectory(destination);
        using (var process = Process.Start(new ProcessStartInfo(payload, "-y -o\"" + destination + "\"") {UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden}))
        {
            process.WaitForExit();
            if (process.ExitCode != 0) throw new IOException("程序解压失败，退出码：" + process.ExitCode);
        }
        string app = Path.Combine(destination, "声年", "声年.exe");
        if (!File.Exists(app) || Sha256(app) != AppHash) throw new IOException("解压后的声年程序校验未通过。");
    }

    private async Task DownloadAndInstall()
    {
        busy = true;
        install.Enabled = browse.Enabled = false;
        string cache = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Shengnian", "Downloads", Version);
        try
        {
            if (Directory.Exists(folder.Text) && Directory.GetFileSystemEntries(folder.Text).Length != 0)
                throw new IOException("安装目录已有文件，请选择新的空目录。若已经安装，可直接打开其中的声年.exe。");
            Directory.CreateDirectory(cache);
            long completed = 0, total = Sizes[0] + Sizes[1];
            for (int index = 0; index < Names.Length; index++)
            {
                string path = Path.Combine(cache, Names[index]);
                if (!await Task.Run(() => ValidPart(path, index)))
                {
                    using (var client = new WebClient())
                    {
                        client.Headers[HttpRequestHeader.UserAgent] = "Shengnian-Installer/0.3.2";
                        int current = index;
                        client.DownloadProgressChanged += delegate(object sender, DownloadProgressChangedEventArgs e) {
                            progress.Value = Math.Min(99, (int)((completed + e.BytesReceived) * 100L / total));
                            status.Text = "正在下载程序 " + (current + 1) + "/2，已完成 " + progress.Value + "%";
                        };
                        await client.DownloadFileTaskAsync(new Uri(BaseUrl + Names[index]), path);
                    }
                    status.Text = "正在校验下载文件 " + (index + 1) + "/2…";
                    if (!await Task.Run(() => ValidPart(path, index))) throw new IOException("下载校验未通过，请重试。");
                }
                completed += Sizes[index];
            }
            status.Text = "正在合并、校验并解压完整程序，请稍候…";
            string destination = folder.Text;
            await Task.Run(() => InstallPayload(cache, cache, destination));
            progress.Value = 100;
            status.Text = "安装完成。启动后点击“API 配置”填写自己的 Key。";
            install.Text = "打开声年";
            install.Enabled = true;
            installedApp = Path.Combine(destination, "声年", "声年.exe");
            if (launch.Checked) Process.Start(new ProcessStartInfo(installedApp) {UseShellExecute = true});
        }
        catch (Exception error)
        {
            status.Text = "安装未完成，可重试。已校验的下载会自动复用。";
            MessageBox.Show(this, error.Message, "声年安装", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            install.Enabled = browse.Enabled = true;
        }
        finally { busy = false; }
    }
}
