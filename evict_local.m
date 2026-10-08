#import <Foundation/Foundation.h>
#import <errno.h>
#import <sys/resource.h>
#import <sys/stat.h>
#import <sys/xattr.h>

static const char *PIN_XATTR = "com.apple.fileprovider.pinned#PX";

static int reply(BOOL ok, NSString *message) {
    NSDictionary *body = @{ @"ok": @(ok), @"message": message ?: @"未知错误" };
    NSData *data = [NSJSONSerialization dataWithJSONObject:body options:0 error:nil];
    if (data != nil) {
        NSString *line = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
        fprintf(stdout, "%s\n", line.UTF8String);
    }
    return ok ? 0 : 1;
}

static int failedEviction(const char *path, const struct stat *original, NSData *pin, NSString *message) {
    if (pin != nil) {
        struct stat current;
        if (lstat(path, &current) != 0 || !S_ISREG(current.st_mode) ||
            current.st_dev != original->st_dev || current.st_ino != original->st_ino ||
            (current.st_flags & SF_DATALESS) != 0) {
            return reply(NO, [message stringByAppendingString:@"；文件状态已变化，请在 Finder 核对"]);
        }
        if (setxattr(path, PIN_XATTR, pin.bytes, pin.length, 0, XATTR_NOFOLLOW) != 0) {
            return reply(NO, [message stringByAppendingString:@"；原有保留下载标记未能恢复，请在 Finder 核对"]);
        }
    }
    return reply(NO, message);
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 2) return reply(NO, @"需要一个文件路径");
        int previousPolicy = getiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
                                             IOPOL_SCOPE_THREAD);
        if (previousPolicy < 0 ||
            setiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES,
                           IOPOL_SCOPE_THREAD,
                           IOPOL_MATERIALIZE_DATALESS_FILES_OFF) != 0) {
            return reply(NO, @"无法启用禁止按需下载策略");
        }
        const char *path = argv[1];
        struct stat info;
        if (lstat(path, &info) != 0 || !S_ISREG(info.st_mode)) {
            return reply(NO, @"目标已不存在或不是普通文件");
        }
        NSString *name = [NSString stringWithUTF8String:path];
        NSURL *url = [NSURL fileURLWithPath:name isDirectory:NO];
        NSError *error = nil;
        NSNumber *ubiquitous = nil;
        NSNumber *uploaded = nil;
        if (![url getResourceValue:&ubiquitous forKey:NSURLIsUbiquitousItemKey error:&error]) {
            return reply(NO, [NSString stringWithFormat:@"无法读取 iCloud 归属属性：%@；本次未改动文件", error.localizedDescription ?: @"未知原因"]);
        }
        if (!ubiquitous.boolValue) {
            return reply(NO, @"macOS 将此路径的 iCloud 归属属性报告为否；可能由 File Provider 管理或状态已变化。本次未改动文件，请重新扫描并在 Finder 核对");
        }
        error = nil;
        if (![url getResourceValue:&uploaded forKey:NSURLUbiquitousItemIsUploadedKey error:&error]) {
            return reply(NO, [NSString stringWithFormat:@"无法读取云端上传状态：%@；已保留本地副本", error.localizedDescription ?: @"未知原因"]);
        }
        if (!uploaded.boolValue) {
            return reply(NO, @"macOS 报告当前版本尚未完成上传，已保留本地副本");
        }

        NSData *pin = nil;
        errno = 0;
        ssize_t pinSize = getxattr(path, PIN_XATTR, NULL, 0, 0, XATTR_NOFOLLOW);
        if (pinSize >= 0) {
            if (pinSize > 65536) return reply(NO, @"保留下载标记异常，已保留本地副本");
            NSMutableData *value = [NSMutableData dataWithLength:(NSUInteger)pinSize];
            if (getxattr(path, PIN_XATTR, value.mutableBytes, value.length, 0, XATTR_NOFOLLOW) != pinSize) {
                return reply(NO, @"无法读取保留下载标记，已保留本地副本");
            }
            if (removexattr(path, PIN_XATTR, XATTR_NOFOLLOW) != 0) {
                return reply(NO, @"无法取消保留下载，已保留本地副本");
            }
            pin = value;
        } else if (errno != ENOATTR) {
            return reply(NO, @"无法检查保留下载标记，已保留本地副本");
        }
        error = nil;
        if (![[NSFileManager defaultManager] evictUbiquitousItemAtURL:url error:&error]) {
            return failedEviction(path, &info, pin, error.localizedDescription ?: @"系统拒绝移除本地下载");
        }
        return reply(YES, @"系统已接受移除本地下载");
    }
}
