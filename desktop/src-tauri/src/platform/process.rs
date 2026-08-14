//! 进程树管理：Windows Job Object / Unix 进程组。

use std::process::{Child, Command};

#[cfg(windows)]
pub mod windows_impl {
    use super::*;

    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{CloseHandle, HANDLE};
    use windows::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows::Win32::System::Threading::{
        OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
    };

    /// 创建 KILL_ON_JOB_CLOSE 的 Job Object。
    /// 句柄泄漏由进程退出兜底（进程关闭时 Job 内所有子进程被内核终止）。
    /// 句柄的内核调用（Assign/Close）线程安全，可跨线程持有。
    pub struct JobHandle(HANDLE);

    // SAFETY: HANDLE 本身可跨线程使用；terminate_tree/drop 只调用一次语义
    // （重复 CloseHandle 仅返回错误，无副作用）。
    unsafe impl Send for JobHandle {}
    unsafe impl Sync for JobHandle {}

    impl JobHandle {
        pub fn create() -> Result<Self, String> {
            unsafe {
                let job = CreateJobObjectW(None, PCWSTR::null())
                    .map_err(|error| format!("job_create_failed:{error}"))?;
                let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const core::ffi::c_void,
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                )
                .map_err(|error| format!("job_configure_failed:{error}"))?;
                Ok(Self(job))
            }
        }

        /// 把已启动的子进程纳入 Job（连同其后续子进程）。
        pub fn assign(&self, pid: u32) -> Result<(), String> {
            unsafe {
                let process = OpenProcess(
                    PROCESS_SET_QUOTA | PROCESS_TERMINATE,
                    false,
                    pid,
                )
                .map_err(|error| format!("job_open_process_failed:{error}"))?;
                let result = AssignProcessToJobObject(self.0, process);
                let _ = CloseHandle(process);
                result.map_err(|error| format!("job_assign_failed:{error}"))
            }
        }

        pub fn terminate_tree(&self) {
            // KILL_ON_JOB_CLOSE：关闭句柄即终止整棵树。主动 CloseHandle。
            unsafe {
                let _ = CloseHandle(self.0);
            }
        }
    }

    impl Drop for JobHandle {
        fn drop(&mut self) {
            self.terminate_tree();
        }
    }

    pub const CREATE_NO_WINDOW: u32 = 0x0800_0000;

    pub fn configure_hidden_command(command: &mut Command) {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }
}

#[cfg(windows)]
pub use windows_impl::{configure_hidden_command, JobHandle};

#[cfg(unix)]
pub mod unix_impl {
    use std::process::Command;

    pub fn configure_new_session(command: &mut Command) {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
}

#[cfg(unix)]
pub use unix_impl::configure_new_session;

/// 终止一个进程树（尽力而为）。
/// Windows：taskkill /T /F；Unix：SIGTERM→等待→SIGKILL 进程组。
pub fn terminate_tree(pid: u32) {
    if pid == 0 {
        return;
    }
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/T", "/F", "/PID", &pid.to_string()])
            .creation_flags_option();
    }
    #[cfg(unix)]
    {
        let _ = Command::new("kill")
            .args(["-TERM", &pid.to_string()])
            .status();
        std::thread::sleep(std::time::Duration::from_millis(1500));
        let _ = Command::new("kill")
            .args(["-KILL", &pid.to_string()])
            .status();
    }
}

#[cfg(windows)]
trait CommandFlags {
    fn creation_flags_option(&mut self) -> &mut Command;
}

#[cfg(windows)]
impl CommandFlags for Command {
    fn creation_flags_option(&mut self) -> &mut Command {
        configure_hidden_command(self);
        self
    }
}

/// 等待子进程退出（用于测试与看门狗）。
pub fn wait_quietly(child: &mut Child) -> std::io::Result<std::process::ExitStatus> {
    child.wait()
}
